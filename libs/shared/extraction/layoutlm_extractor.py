"""
LayoutLM-based field extraction with OCR token alignment.

Runs LayoutLM Document QA on each page image, asking targeted questions
for each field. Aligns extracted answers back to OCR tokens for
evidence bounding boxes.

This is the primary VLM extraction method — works out-of-box and
can be fine-tuned with review corrections.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from libs.shared.db.models.enums import ExtractionMethodEnum
from libs.shared.extraction.field_builder import ExtractionCandidate
from libs.shared.vlm.layoutlm_client import FIELD_QUESTIONS_VARIANTS

# All known field keys (from variants dict)
FIELD_QUESTIONS_KEYS = list(FIELD_QUESTIONS_VARIANTS.keys())

logger = logging.getLogger(__name__)

# Confidence bounds
LAYOUTLM_CONFIDENCE_MIN = 0.10
LAYOUTLM_CONFIDENCE_MAX = 0.98

# ── Strict field-type validation patterns ──────────────────────────────
_DATE_PART = r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"
_DATE_RE = re.compile(rf"({_DATE_PART})")

# Service/CPT/HCPCS codes: letter(s)+digits or 5-digit numeric
# Examples: G0480, G0481, G0482, H2034, H2036, 99213, T1015
_SERVICE_CODE_RE = re.compile(r"^[A-Z]\d{3,4}$|^\d{5}$")

_NPI_RE = re.compile(r"^\d{10}$")

_PHONE_RE = re.compile(
    r"^\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}$"
)

# Prose / label words that should never be a field value
_PROSE_WORDS = frozenset({
    "verification", "eligibility", "contractual", "please", "https",
    "available", "benefits", "coverage", "guidelines", "department",
    "sincerely", "seriously", "information", "services", "medical",
    "language", "people", "free", "provide", "address", "contact",
    "regarding", "original", "documents", "return", "above",
})

_LABEL_WORDS = frozenset({
    "memberid", "member", "subscriber", "enrollee", "provider",
    "authorization", "phone", "fax", "date", "name", "npi",
})


@dataclass
class LayoutLMExtractionResult:
    """Result from LayoutLM-based extraction across pages."""

    fields: dict[str, ExtractionCandidate] = field(default_factory=dict)
    model_name: str = ""
    latency_ms: float = 0.0
    pages_processed: int = 0
    raw_answers: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def field_count(self) -> int:
        return len(self.fields)

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_count": self.field_count,
            "model_name": self.model_name,
            "latency_ms": round(self.latency_ms, 1),
            "pages_processed": self.pages_processed,
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "raw_answers": self.raw_answers,
        }


@dataclass
class OcrTokenForAlignment:
    """OCR token for evidence alignment."""
    text: str
    x0: float  # normalized 0-1
    y0: float
    x1: float
    y1: float
    confidence: float


class LayoutLMExtractor:
    """
    Extracts fields from page images using LayoutLM Document QA.

    For each field, asks a targeted question. The model extracts
    the answer directly from the document image + text.

    Workflow per page:
      1. Convert OCR tokens to word_boxes format for the pipeline.
      2. For each field, ask a question and get the answer.
      3. Fuzzy-match the answer back to OCR tokens for bbox evidence.
      4. Build ExtractionCandidate with evidence.

    Args:
        layoutlm_client: A LayoutLMClient instance.
        max_pages: Maximum pages to process (content pages only).
        min_confidence: Minimum score to accept an answer.
    """

    def __init__(
        self,
        layoutlm_client: Any,
        max_pages: int = 3,
        min_confidence: float = 0.05,
    ):
        self.client = layoutlm_client
        self.max_pages = max_pages
        self.min_confidence = min_confidence

    def extract(
        self,
        page_images: list[np.ndarray],
        ocr_tokens_by_page: list[list[OcrTokenForAlignment]] | None = None,
        payer_name: str | None = None,
        fields_to_query: list[str] | None = None,
    ) -> LayoutLMExtractionResult:
        """
        Extract fields from page images using LayoutLM.

        Args:
            page_images: List of page images (BGR numpy arrays).
            ocr_tokens_by_page: OCR tokens per page for word_boxes + alignment.
            payer_name: Detected payer (for logging).
            fields_to_query: If provided, only ask LayoutLM about these fields
                (gap-fill mode).  None means query all known fields.

        Returns:
            LayoutLMExtractionResult with extracted candidates.
        """
        if not page_images:
            return LayoutLMExtractionResult()

        start = time.monotonic()
        images_to_process = page_images[:self.max_pages]

        # Determine which fields to ask about; default = all known fields.
        _base_fields = fields_to_query if fields_to_query is not None else FIELD_QUESTIONS_KEYS

        all_fields: dict[str, ExtractionCandidate] = {}
        raw_answers: dict[str, list[dict]] = {}

        for page_idx, page_image in enumerate(images_to_process):
            # Build word_boxes from OCR tokens (LayoutLM needs 0-1000 range)
            word_boxes = None
            page_tokens: list[OcrTokenForAlignment] = []
            if ocr_tokens_by_page and page_idx < len(ocr_tokens_by_page):
                page_tokens = ocr_tokens_by_page[page_idx]
                word_boxes = [
                    (t.text, [int(t.x0 * 1000), int(t.y0 * 1000), int(t.x1 * 1000), int(t.y1 * 1000)])
                    for t in page_tokens
                    if t.text.strip()
                ]

            # Only try fields not yet resolved (within the requested set)
            remaining = [k for k in _base_fields if k not in all_fields]
            if not remaining:
                break

            try:
                # Prefer multi-question extract_candidates() for better recall
                if hasattr(self.client, "extract_candidates"):
                    candidates_map = self.client.extract_candidates(
                        page_image,
                        field_keys=remaining,
                        word_boxes=word_boxes,
                        top_k=3,
                    )
                else:
                    vlm_response = self.client.extract(
                        page_image,
                        field_keys=remaining,
                        word_boxes=word_boxes,
                    )
                    candidates_map = {
                        k: [(v, 0.5)] for k, v in vlm_response.fields.items() if v
                    }

                for field_key, cand_list in candidates_map.items():
                    if field_key in all_fields:
                        continue

                    if field_key not in raw_answers:
                        raw_answers[field_key] = []

                    # Try each candidate (best model score first) until one
                    # passes field-type validation
                    for raw_value, model_score in cand_list:
                        raw_value = str(raw_value)

                        raw_answers[field_key].append({
                            "page": page_idx + 1,
                            "raw_value": raw_value,
                            "model_score": round(model_score, 4),
                        })

                        cleaned = _validate_and_clean_value(field_key, raw_value)
                        if cleaned is None:
                            raw_answers[field_key][-1]["rejected"] = True
                            raw_answers[field_key][-1]["reason"] = "failed_validation"
                            logger.debug(
                                "LayoutLM REJECTED %s='%s' (score=%.3f, page=%d)",
                                field_key, raw_value, model_score, page_idx + 1,
                            )
                            continue

                        if cleaned != raw_value:
                            logger.info(
                                "LayoutLM cleaned %s: '%s' -> '%s'",
                                field_key, raw_value, cleaned,
                            )
                        raw_answers[field_key][-1]["cleaned_value"] = cleaned

                        # Align to OCR tokens for evidence bbox
                        evidence_bbox = None
                        evidence_text = cleaned
                        ocr_align_conf = 0.5

                        if page_tokens:
                            matched = self._find_matching_tokens(cleaned, page_tokens)
                            if matched:
                                evidence_bbox = {
                                    "x0": min(t.x0 for t in matched),
                                    "y0": min(t.y0 for t in matched),
                                    "x1": max(t.x1 for t in matched),
                                    "y1": max(t.y1 for t in matched),
                                    "page": page_idx + 1,
                                }
                                evidence_text = " ".join(t.text for t in matched)
                                ocr_align_conf = sum(t.confidence for t in matched) / len(matched)

                        # Blend model score (60%) + OCR alignment (40%).
                        # Previously only used OCR alignment — now model score
                        # is the primary signal for LayoutLM extractive QA.
                        blended = model_score * 0.60 + ocr_align_conf * 0.40
                        confidence = max(
                            LAYOUTLM_CONFIDENCE_MIN,
                            min(LAYOUTLM_CONFIDENCE_MAX, blended),
                        )

                        all_fields[field_key] = ExtractionCandidate(
                            value=cleaned,
                            method=ExtractionMethodEnum.LAYOUTLM,
                            confidence=confidence,
                            evidence_bbox=evidence_bbox,
                            evidence_text=evidence_text,
                        )
                        break  # stop at first validated candidate

            except Exception as e:
                logger.warning(
                    "LayoutLM extraction failed on page %d: %s",
                    page_idx + 1, e,
                )

        latency = (time.monotonic() - start) * 1000

        logger.info(
            "LayoutLM extraction complete: %d fields from %d pages in %.0f ms",
            len(all_fields), len(images_to_process), latency,
        )

        return LayoutLMExtractionResult(
            fields=all_fields,
            model_name=self.client.config.model_name,
            latency_ms=latency,
            pages_processed=len(images_to_process),
            raw_answers=raw_answers,
        )

    @staticmethod
    def _find_matching_tokens(
        value: str,
        tokens: list[OcrTokenForAlignment],
    ) -> list[OcrTokenForAlignment]:
        """Find OCR tokens that match the extracted value."""
        if not value or not tokens:
            return []

        value_lower = value.lower().strip()
        value_words = value_lower.split()

        if not value_words:
            return []

        matched: list[OcrTokenForAlignment] = []

        for word in value_words:
            best_token = None
            best_score = 0.0

            for token in tokens:
                token_text = token.text.lower().strip()
                if not token_text:
                    continue

                # Exact match
                if token_text == word:
                    best_token = token
                    best_score = 1.0
                    break

                # Substring match (token contains word or vice versa)
                if word in token_text or token_text in word:
                    score = min(len(word), len(token_text)) / max(len(word), len(token_text))
                    if score > best_score:
                        best_score = score
                        best_token = token

            if best_token and best_score > 0.5:
                matched.append(best_token)

        return matched


# ── Strict field-value validation (module-level) ───────────────────────

def _validate_and_clean_value(field_key: str, raw_value: str) -> str | None:
    """Validate and clean a LayoutLM-extracted value by field type.

    Returns the cleaned value, or None if the value fails validation
    (meaning it should be rejected entirely — LayoutLM hallucinated or
    grabbed the wrong text).

    Mirrors the strict validation in template_extractor._trim_value_by_type
    and ocr_label_extractor._filter_tokens_by_field_type.
    """
    if not raw_value or not raw_value.strip():
        return None

    value = raw_value.strip()
    key = field_key.lower()

    # ── Dates (patient_dob, auth_effective_date, auth_expiration_date, etc.)
    if key in ("patient_dob", "auth_effective_date", "auth_expiration_date", "next_review_date"):
        dates = _DATE_RE.findall(value)
        if not dates:
            return None  # No date pattern found → reject
        if "expir" in key or "end" in key:
            return dates[-1]  # Last date for expiration
        return dates[0]  # First date for effective/DOB

    # ── Service / CPT / HCPCS codes
    if key in ("service_code", "cpt_code", "hcpc_code"):
        # Extract individual code tokens
        codes = []
        for word in value.split():
            clean = word.strip("()[]{}.,;: ")
            if _SERVICE_CODE_RE.match(clean.upper()):
                codes.append(clean.upper())
        if not codes:
            return None  # No valid code pattern → reject
        return " ".join(codes)

    # ── Provider NPI
    if key in ("provider_npi", "npi"):
        digits = re.sub(r"\D", "", value)
        if len(digits) == 10:
            return digits
        # Try to find a 10-digit sequence inside the value
        m = re.search(r"\d{10}", value)
        if m:
            return m.group(0)
        return None  # Not a valid NPI → reject

    # ── Phone / Fax numbers
    if key in ("provider_phone", "provider_fax"):
        # Strip common prefixes
        cleaned = re.sub(r"^(?:phone|fax|tel|ph)[:\s#]*", "", value, flags=re.IGNORECASE).strip()
        digits = re.sub(r"\D", "", cleaned)
        if len(digits) == 10:
            return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
        if len(digits) == 11 and digits[0] == "1":
            d = digits[1:]
            return f"({d[:3]}) {d[3:6]}-{d[6:]}"
        return None  # Not a valid phone → reject

    # ── Prior auth / reference number
    if key in ("prior_auth_number", "reference_number"):
        if len(value) > 35:
            return None  # Too long — grabbed prose
        if "#" in value:
            after = value.split("#", 1)[1].strip()
            if after and any(c.isdigit() for c in after):
                value = after
        # Must contain at least one digit
        if not any(c.isdigit() for c in value):
            return None
        # Reject phone-number patterns (XXX-XXX-XXXX or (XXX) XXX-XXXX)
        digits_only = re.sub(r"\D", "", value)
        if len(digits_only) == 10 and _PHONE_RE.match(value.strip()):
            return None
        # Reject if it looks like a 10-digit phone in any format
        if re.match(r"^\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}$", value.strip()):
            return None
        # Reject toll-free / 1-8xx phone patterns (1-866-246-4358, 1-800-xxx-xxxx)
        if re.match(r"^1[\s\-.]?8\d{2}[\s\-.]?\d{3}[\s\-.]?\d{4}$", value.strip()):
            return None
        # Reject 11-digit phone numbers starting with 1
        if len(digits_only) == 11 and digits_only[0] == "1":
            # If remaining 10 digits look phone-like, reject
            remaining = digits_only[1:]
            if remaining[:3] in ("800", "866", "877", "888", "855", "844", "833"):
                return None
        # Reject prose
        val_lower = value.lower()
        if any(w in val_lower for w in _PROSE_WORDS):
            return None
        # Keep only the first ID-like token(s)
        parts = value.split()
        id_parts = []
        for p in parts:
            clean = p.strip("()[]{}.,;:")
            if any(c.isdigit() for c in clean) and re.match(r"^[A-Za-z0-9\-#]+$", clean) and len(clean) >= 3:
                id_parts.append(clean)
            else:
                break
        return " ".join(id_parts) if id_parts else None

    # ── Member ID
    if key == "member_id":
        # Strip label-like prefixes: "#:", "ID:", "Member:", etc.
        cleaned = re.sub(r"^[#:=\s]+", "", value).strip()
        if not cleaned:
            return None
        if len(cleaned) > 30:
            return None
        if not any(c.isdigit() for c in cleaned):
            return None
        val_lower = cleaned.lower()
        reject = ("date span", "review", "next", "authorization", "span",
                  "member", "phone", "provider", "enrollee", "plan id",
                  "subscriber", "medicaid")
        if any(w in val_lower for w in reject):
            return None
        if any(w in val_lower for w in _PROSE_WORDS):
            return None
        # Return first token that contains digits
        for part in cleaned.split():
            clean_part = part.strip("#:=()[]{}.,;")
            if clean_part and any(c.isdigit() for c in clean_part):
                return clean_part
        return None

    # ── Patient name
    if key == "patient_name":
        if len(value) < 2:
            return None
        # Strip fax header prefixes like "O:", "To:", "From:", "rom:"
        cleaned = re.sub(r"^(?:O|To|From|rom|Attn)[:\s]+", "", value, flags=re.IGNORECASE).strip()
        # Strip leading/trailing dates (numeric format)
        cleaned = re.sub(r"^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\s+", "", cleaned)
        cleaned = re.sub(r"\s+\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}.*$", "", cleaned)
        cleaned = re.sub(r"^\d{6,}\s+", "", cleaned)
        # Reject text-format dates like "November 15, 2024", "Jan 5, 2025"
        if re.match(
            r"^(?:January|February|March|April|May|June|July|August|September|"
            r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\s+\d{1,2},?\s+\d{4}$",
            cleaned.strip(), re.IGNORECASE,
        ):
            return None
        cleaned = cleaned.strip()
        if not cleaned or len(cleaned) < 2:
            return None
        words = re.split(r"[\s,]+", cleaned)
        # Reject label-like text
        if any(w.lower().rstrip(":,#") in _LABEL_WORDS for w in words):
            return None
        # Reject organization/company names (not a patient name)
        cl = cleaned.lower()
        _org_indicators = ("llc", "inc", "corp", "ltd", "healthcare", "clinical",
                           "services", "hospital", "medical", "center", "health",
                           "caresource", "molina", "anthem", "humana", "buckeye",
                           "amerihealth", "paramount", "promedica", "aetna",
                           "unitedhealth")
        cl_words = set(re.split(r"[\s,.\-]+", cl))
        if cl_words & set(_org_indicators):
            return None
        # Reject prose
        if sum(1 for w in _PROSE_WORDS if w in cl) >= 2:
            return None
        return cleaned

    # ── Provider name
    if key == "provider_name":
        if len(value) < 3:
            return None
        # Strip fax header prefixes
        cleaned = re.sub(r"^(?:O|To|From|rom|Attn)[:\s]+", "", value, flags=re.IGNORECASE).strip()
        # Strip "Company:" / "Provider:" / "Name:" prefixes
        cleaned = re.sub(r"^(?:Company|Provider|Name|Facility)[:\s]+", "", cleaned, flags=re.IGNORECASE).strip()
        if len(cleaned) < 3:
            return None
        # Reject if too long (grabbed a sentence instead of a name)
        if len(cleaned) > 80:
            return None
        words = cleaned.split()
        if len(words) < 1:
            return None
        val_lower = cleaned.lower()
        reject = ("sincerely", "seriously", "dear", "regards", "attention",
                  "department", "yours truly", "location address",
                  "service location", "risk your", "physical health",
                  "coverage", "benefits", "eligibility",
                  "please contact", "free language",
                  "authorization", "authorized", "mycare hours",
                  "customer care", "menu option")
        if any(w in val_lower for w in reject):
            return None
        # Reject if the provider name IS a payer org (LayoutLM grabbed the wrong entity)
        _payer_orgs = ("caresource", "molina healthcare", "anthem", "humana",
                       "buckeye health", "amerihealth", "paramount advantage",
                       "promedica", "aetna", "unitedhealth", "united health")
        if any(org in val_lower for org in _payer_orgs):
            return None
        # Reject ".com" domain names as provider names
        if val_lower.endswith(".com") and len(words) == 1:
            return None
        return cleaned

    # ── Diagnosis code (ICD-10)
    if key == "diagnosis_codes":
        # ICD-10: letter + digits, optional dot
        codes = []
        for word in value.split():
            clean = word.strip("()[]{}.,;: ")
            if re.match(r"^[A-Z]\d{2,4}(?:\.\d{0,4}[A-Z]?)?$", clean.upper()):
                codes.append(clean.upper())
        if not codes:
            return None
        return " ".join(codes)

    # ── Units requested
    if key == "units_requested":
        if len(value) > 25:
            return None
        # Must contain at least one digit
        if not any(c.isdigit() for c in value):
            return None
        val_lower = value.lower()
        if any(w in val_lower for w in _PROSE_WORDS):
            return None
        # Extract numeric portion (including decimals like 3.5)
        nums = re.findall(r"\d+(?:\.\d+)?", value)
        if nums:
            return " ".join(nums)
        return None

    # ── Default: basic sanity check (reject very long or pure-prose values)
    if len(value) > 100:
        return None
    val_lower = value.lower()
    if sum(1 for w in _PROSE_WORDS if w in val_lower) >= 3:
        return None
    return value
