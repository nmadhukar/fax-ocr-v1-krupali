"""
OCR label-based heuristic field extraction (fallback).

When no template matches, this extractor searches OCR tokens for known
field labels (e.g., "Member ID", "Authorization Number") and extracts
the value tokens to the right or below the label.

This serves as a general-purpose fallback for the template-based primary
extraction path.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from libs.shared.db.models.enums import ExtractionMethodEnum
from libs.shared.extraction.field_builder import ExtractionCandidate

logger = logging.getLogger(__name__)

# Confidence penalty applied to OCR-label extractions (no template = less trust)
OCR_LABEL_CONFIDENCE_PENALTY = 0.10

# Map field_key → list of label aliases (case-insensitive matching)
LABEL_ALIASES: dict[str, list[str]] = {
    "member_id": [
        "Member ID",
        "Member ID#",
        "Member ID #",
        "Member Number",
        "Medicaid ID",
        "Medicaid ID#",
        "Medicaid ID #",
        "Subscriber ID",
        "Health Plan ID",
        "Member plan ID number",
        "Plan ID",
    ],
    "prior_auth_number": [
        "Authorization Number",
        "Auth Number",
        "Reference Number",
        "Reference#",
        "Reference #",
        "Prior Auth Number",
        "PA Number",
        "Humana Authorization Number",
    ],
    "patient_name": [
        "Patient Name",
        "Member Name",
        "Enrollee Name",
        "Insured Name",
    ],
    "patient_dob": [
        "Date of Birth",
        "Date of birth",
        "DOB",
        "Member DOB",
        "Member Date of Birth",
        "Birth Date",
    ],
    "provider_name": [
        "Provider Name",
        "Requesting Provider",
        "Requesting Provider Name",
        "Ordering Provider Name",
        "Attending Provider",
        "Facility Name",
        "Provider or facility",
    ],
    "provider_npi": [
        "NPI",
        "NPI#",
        "NPI #",
        "Provider NPI",
        "National Provider Identifier",
    ],
    "provider_phone": [
        "Provider Phone",
        "Phone Number",
        "Telephone Number",
        "Contact Number",
    ],
    "provider_fax": [
        "Provider Fax",
        "Fax Number",
    ],
    "auth_effective_date": [
        "Date Span",  # First: Molina uses inline "Date Span08/25/2025-09/16/2025"
        "Effective Date",
        "Start Date",
        "Admission Date",
        "Service Start Date",
        "Admission/Service Start Date",
        "Authorization Dates",
        "Dates of service",
        "Date(s) Requested",
    ],
    "auth_expiration_date": [
        "Expiration Date",
        "End Date",
        "Discharge Date",
        "Dates Approved",
        "Authorization Through",
        "Authorized / Denied Days",
        "Authorized/Denied Days",
        "Date Span",
    ],
    "service_code": [
        "Service Code",
        "CPT Code",
        "HCPC Code",
        "HCPCS",
        "Procedure Code",
        "HCPC/CPT codes",
    ],
    "diagnosis_codes": [
        "Diagnosis Code",
        "ICD-10",
        "ICD Code",
        "DX Code",
        "Primary Diagnosis",
    ],
    "units_requested": [
        "Units Requested",
        "Approved Units",
        "Number of Units",
        "Denied Units",
    ],
    "next_review_date": [
        "Next Review Date",
    ],
}


@dataclass
class OcrTokenSimple:
    """Lightweight OCR token for label matching."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float
    line_number: int


def extract_fields_from_ocr_tokens(
    tokens_by_page: dict[int, list[Any]],
    field_keys: list[str] | None = None,
    payer_name: str | None = None,
) -> dict[str, ExtractionCandidate]:
    """
    Extract fields from OCR tokens using label-based heuristic matching.

    For each requested field, searches OCR tokens for known label strings,
    then collects value tokens to the right or below the label.

    Args:
        tokens_by_page: {page_number: [FaxOcrToken or OcrTokenSimple, ...]}.
        field_keys: Fields to extract (default: all in LABEL_ALIASES).
        payer_name: Detected payer (for logging).

    Returns:
        Dict of field_key → ExtractionCandidate.
    """
    if field_keys is None:
        field_keys = list(LABEL_ALIASES.keys())

    # Build all known label words for page scoring
    all_label_phrases = set()
    for aliases in LABEL_ALIASES.values():
        for alias in aliases:
            all_label_phrases.add(alias.lower().rstrip(":").rstrip("#"))

    # Pre-normalize all pages
    normalized_pages: list[tuple[int, list[OcrTokenSimple]]] = []
    for page_num, page_tokens in tokens_by_page.items():
        if not page_tokens:
            continue
        simple_tokens = _normalize_tokens(page_tokens)
        if not simple_tokens:
            continue
        normalized_pages.append((page_num, simple_tokens))

    # Score each page by how many field labels it contains (form pages > boilerplate)
    def _page_label_score(tokens: list[OcrTokenSimple]) -> int:
        page_text = " ".join(t.text.lower() for t in tokens)
        score = 0
        for phrase in all_label_phrases:
            if phrase in page_text:
                score += 1
        return score

    # Skip cover pages (contain "fax cover", very few tokens, or boilerplate)
    def _is_cover_or_boilerplate(tokens: list[OcrTokenSimple]) -> bool:
        page_text = " ".join(t.text.lower() for t in tokens)
        if "fax cover" in page_text or "cover page" in page_text:
            return True
        if "confidentiality statement" in page_text and len(tokens) < 30:
            return True
        return False

    # Sort pages: structured form pages first (highest label count), skip covers
    scored_pages = []
    for page_num, tokens in normalized_pages:
        if _is_cover_or_boilerplate(tokens):
            continue
        score = _page_label_score(tokens)
        scored_pages.append((score, page_num, tokens))

    # Sort by label score descending (form pages first)
    scored_pages.sort(key=lambda x: -x[0])

    results: dict[str, ExtractionCandidate] = {}

    for _score, page_num, simple_tokens in scored_pages:
        for field_key in field_keys:
            if field_key in results:
                continue  # Already found on a better page

            labels = LABEL_ALIASES.get(field_key, [])
            if not labels:
                continue

            candidate = _find_field_by_label(
                field_key, labels, simple_tokens
            )
            if candidate:
                if candidate.evidence_bbox is None:
                    candidate.evidence_bbox = {}
                candidate.evidence_bbox["page"] = page_num
                results[field_key] = candidate

    if results:
        logger.info(
            "OCR label extraction: found %d/%d fields (payer=%s)",
            len(results), len(field_keys), payer_name or "unknown",
        )

    return results


def _normalize_tokens(tokens: list[Any]) -> list[OcrTokenSimple]:
    """Convert DB tokens or any token format to OcrTokenSimple."""
    simple: list[OcrTokenSimple] = []

    for tok in tokens:
        # FaxOcrToken (DB model) — has token_text, bbox_x0 etc.
        if hasattr(tok, "token_text"):
            simple.append(OcrTokenSimple(
                text=tok.token_text,
                x0=float(tok.bbox_x0),
                y0=float(tok.bbox_y0),
                x1=float(tok.bbox_x1),
                y1=float(tok.bbox_y1),
                confidence=float(tok.confidence),
                line_number=tok.line_number,
            ))
        # Dict format
        elif isinstance(tok, dict):
            simple.append(OcrTokenSimple(
                text=tok.get("text", tok.get("token_text", "")),
                x0=float(tok.get("x0", tok.get("bbox_x0", 0))),
                y0=float(tok.get("y0", tok.get("bbox_y0", 0))),
                x1=float(tok.get("x1", tok.get("bbox_x1", 0))),
                y1=float(tok.get("y1", tok.get("bbox_y1", 0))),
                confidence=float(tok.get("confidence", 0)),
                line_number=int(tok.get("line_number", 0)),
            ))
        # PaddleOCR OcrToken — has .text, .bbox (BoundingBox with .x0/.y0/.x1/.y1)
        elif hasattr(tok, "text") and hasattr(tok, "bbox"):
            bbox = tok.bbox
            simple.append(OcrTokenSimple(
                text=tok.text,
                x0=float(bbox.x0),
                y0=float(bbox.y0),
                x1=float(bbox.x1),
                y1=float(bbox.y1),
                confidence=float(tok.confidence),
                line_number=getattr(tok, "line_number", 0),
            ))
        # OcrTokenSimple or flat dataclass with direct x0/y0/x1/y1
        elif hasattr(tok, "text") and hasattr(tok, "x0"):
            simple.append(OcrTokenSimple(
                text=tok.text,
                x0=float(tok.x0),
                y0=float(tok.y0),
                x1=float(tok.x1),
                y1=float(tok.y1),
                confidence=float(tok.confidence),
                line_number=getattr(tok, "line_number", 0),
            ))
        else:
            logger.debug(
                "Unrecognized token type %s in _normalize_tokens — skipped",
                type(tok).__name__,
            )

    return simple


_DATE_PART = r"(?:\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}[/\-]\d{1,2}[/\-]\d{1,2})"
_DATE_RE = re.compile(
    rf"^{_DATE_PART}"                           # Base date (MM/DD/YYYY or YYYY-MM-DD)
    rf"(?:[- ]*(?:to|through|-)\s*{_DATE_PART})?$"  # Optional range
)
_EMBEDDED_DATE_RE = re.compile(_DATE_PART)
_PHONE_RE = re.compile(r"[\d\(\)\-\.\s]{7,}")
_NPI_RE = re.compile(r"^\d{10}$")

_DATE_PARSE_FORMATS = (
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%m/%d/%y",
    "%m-%d-%y",
    "%Y/%m/%d",
    "%Y-%m-%d",
)


_LABEL_PREFIX_RE: dict[str, "re.Pattern[str]"] = {
    "member_id": re.compile(
        r"^(?:member\s*(?:id|#|number|plan\s*id)|medicaid\s*id|"
        r"subscriber\s*id|plan\s*id|id\s*#?|enrollee\s*id)\s*[:=\s]+",
        re.IGNORECASE,
    ),
    "patient_dob": re.compile(
        r"^(?:member\s*date\s*of\s*birth|date\s*of\s*birth|"
        r"patient\s*(?:date\s*of\s*birth|dob)|dob|birth\s*date)\s*[:=\s]+",
        re.IGNORECASE,
    ),
    "patient_name": re.compile(
        r"^(?:member\s*name|patient\s*name|subscriber\s*name|"
        r"patient|member|name)\s*[:=\s]+",
        re.IGNORECASE,
    ),
    "provider_name": re.compile(
        r"^(?:servicing\s*provider\s*(?:name)?|requesting\s*provider\s*(?:name)?|"
        r"provider\s*(?:name)?|facility\s*(?:name)?|treating\s*provider)\s*[:=.,\s]+",
        re.IGNORECASE,
    ),
    "prior_auth_number": re.compile(
        r"^(?:authorization\s*(?:number|#|no\.?)|auth\s*(?:number|#|no\.?)|"
        r"reference\s*(?:number|#)|prior\s*auth\s*(?:number|#)?)\s*[:=\s]+",
        re.IGNORECASE,
    ),
}


_LEADING_SEP_RE = re.compile(r"^[\s:=.|;,]+")


def _strip_leading_separator(value: str) -> str:
    """Strip leading separator chars left from OCR tokenization (': VALUE' → 'VALUE')."""
    if not value:
        return value
    stripped = _LEADING_SEP_RE.sub("", value).strip()
    return stripped if stripped else value


def _strip_label_prefix(field_key: str, value: str) -> str:
    """Strip common label prefixes from extracted values (safety net for inline/token paths).

    Returns empty string when the entire value is a label prefix with no actual value
    (e.g., "Servicing Provider Name:." → "").  The caller should treat "" as no value.
    """
    pat = _LABEL_PREFIX_RE.get(field_key.lower())
    if not pat:
        return _strip_leading_separator(value)
    stripped = pat.sub("", value, count=1).strip()
    stripped = _strip_leading_separator(stripped)
    # Return stripped (may be "") — do NOT fall back to original when pattern matched everything
    return stripped


def _normalize_date_value_for_field(field_key: str, value: str) -> str:
    """Normalize date/range values based on the target field semantics."""
    def _is_valid_date_token(token: str) -> bool:
        for fmt in _DATE_PARSE_FORMATS:
            try:
                datetime.strptime(token, fmt)
                return True
            except ValueError:
                continue
        return False

    key = field_key.lower()
    if key not in ("patient_dob", "auth_effective_date", "auth_expiration_date", "next_review_date"):
        return value

    all_dates = [d for d in _EMBEDDED_DATE_RE.findall(value or "") if _is_valid_date_token(d)]
    if not all_dates:
        return ""

    if key == "auth_expiration_date":
        return all_dates[-1]
    return all_dates[0]


def _ocr_normalize(text: str) -> str:
    """Normalize common OCR character confusions for label matching.

    Only applied to alphabetic label text — NOT to values containing digits.
    PaddleOCR often confuses: O↔0, I/l↔1 in labels.
    """
    result = []
    for ch in text:
        if ch == "0" and not any(c.isdigit() for c in text if c != ch):
            result.append("o")
        elif ch == "1" and not any(c.isdigit() for c in text if c != ch):
            result.append("l")
        else:
            result.append(ch)
    return "".join(result)


def _filter_tokens_by_field_type(
    field_key: str, tokens: list[OcrTokenSimple]
) -> list[OcrTokenSimple]:
    """Filter value tokens based on expected field type patterns.

    Removes tokens that clearly don't belong to the field type, e.g.,
    date-like tokens in a member_id field.
    """
    if not tokens:
        return tokens

    if field_key in ("patient_dob", "auth_effective_date", "auth_expiration_date", "next_review_date"):
        # Keep date-like tokens and separators — strict
        filtered = []
        for t in tokens:
            text = t.text.strip().rstrip(",").rstrip(":")
            if _DATE_RE.match(text):
                filtered.append(t)
            elif text.lower() in ("to", "-", "through", "thru"):
                filtered.append(t)
            elif _EMBEDDED_DATE_RE.search(text):
                # Token contains embedded date (e.g., "Authorized:8/16/2025-9/14/2025")
                # Extract ONLY the date portion(s), not surrounding text
                date_matches = _EMBEDDED_DATE_RE.findall(text)
                if date_matches:
                    # Build clean date string: "8/16/2025-9/14/2025" or "11/07/2024"
                    # Check for range separators between dates
                    clean_date = date_matches[0]
                    if len(date_matches) >= 2:
                        # Find separator between first and second date
                        idx1_end = text.find(date_matches[0]) + len(date_matches[0])
                        idx2_start = text.find(date_matches[1], idx1_end)
                        between = text[idx1_end:idx2_start].strip()
                        sep = between if between in ("-", "to", "through", "thru") else "-"
                        clean_date = f"{date_matches[0]}{sep}{date_matches[1]}"
                    synthetic = OcrTokenSimple(
                        text=clean_date, x0=t.x0, y0=t.y0,
                        x1=t.x1, y1=t.y1,
                        confidence=t.confidence, line_number=t.line_number,
                    )
                    filtered.append(synthetic)
        return filtered  # Strict: no fallback, dates must match pattern

    if field_key == "member_id":
        # Remove date-like tokens from member ID values
        filtered = [t for t in tokens if not _DATE_RE.match(t.text.strip())]
        combined = " ".join(t.text.strip() for t in filtered)
        # Member IDs are short alphanumeric strings (max ~25 chars)
        if len(combined) > 30:
            return []
        # Must be at least 4 chars (relaxed from 6 — some payers use short IDs)
        if len(combined) < 4:
            return []
        # Must contain at least one digit (IDs are numeric/alphanumeric)
        if not any(c.isdigit() for c in combined):
            return []
        # Reject if contains label-like text or other field labels
        combined_lower = combined.lower()
        _member_reject = ("date span", "review", "next", "authorization", "span",
                          "member", "phone", "provider", "enrollee", "plan id",
                          "subscriber", "medicaid")
        if any(w in combined_lower for w in _member_reject):
            return []
        return filtered

    if field_key == "provider_npi":
        # Keep only 10-digit numbers — strict, no fallback
        return [t for t in tokens if _NPI_RE.match(t.text.strip())]

    if field_key == "prior_auth_number":
        # Reject values that contain other field labels (cross-field contamination)
        combined = " ".join(t.text.strip() for t in tokens)
        combined_lower = combined.lower()
        # Reject if too long (auth numbers are typically <25 chars)
        if len(combined) > 30:
            return []
        # Reject if contains strong prose words (grabbed boilerplate/policy text)
        # Reduced from 10 to 5 most obvious prose indicators
        _prose_words = ("verification", "eligibility", "contractual",
                        "please", "https")
        if any(w in combined_lower for w in _prose_words):
            return []
        for t in tokens:
            text_lower = t.text.strip().lower()
            if any(lbl in text_lower for lbl in ("member dob", "member name", "date of birth", "member number")):
                return []
        return tokens

    if field_key == "units_requested":
        # Units are short numeric values (e.g., "30", "15 15", "120")
        combined = " ".join(t.text.strip() for t in tokens)
        # Reject if too long (units are typically <20 chars)
        if len(combined) > 25:
            return []
        # Must contain at least one digit
        if not any(c.isdigit() for c in combined):
            return []
        # Reject prose words
        _units_prose = ("above", "return", "original", "documents", "address",
                        "guidelines", "available", "benefits", "please", "verification")
        combined_lower = combined.lower()
        if any(w in combined_lower for w in _units_prose):
            return []
        return tokens

    if field_key == "diagnosis_codes":
        # ICD-10 format: letter followed by digits and optional dot — strict
        filtered = [t for t in tokens if re.match(r"^[A-Z]\d", t.text.strip())]
        return filtered  # Strict: no fallback, must match ICD pattern

    if field_key == "service_code":
        # CPT/HCPCS codes: 3-7 char alphanumeric (may include dots for levels)
        # Min 3 chars accepts modifiers like "TG"; also handles "H0015 TG"
        filtered = []
        for t in tokens:
            text = t.text.strip()
            for word in text.split():
                clean = word.strip("()[]{}.,;:")
                if (re.match(r"^[A-Z0-9.]{3,7}$", clean)
                        and any(c.isdigit() for c in clean)):
                    filtered.append(t)
                    break
        return filtered

    if field_key == "patient_name":
        combined = " ".join(t.text.strip() for t in tokens)
        combined_lower = combined.lower()
        # Accept single-word names (some faxes have "SMITH" only)
        # Split by both space and comma to handle "Minarik,Annie" format
        name_parts = [w for w in re.split(r"[\s,]+", combined) if len(w) > 0]
        if len(name_parts) < 1 or len(combined) < 2:
            return []
        # Reject ID-like strings masquerading as names (e.g., "0806WD89S").
        compact = re.sub(r"[^A-Za-z0-9]", "", combined)
        if compact:
            digit_ratio = sum(1 for c in compact if c.isdigit()) / len(compact)
            if digit_ratio > 0.35:
                return []
            if re.match(r"^[A-Z0-9]{6,}$", compact) and any(c.isdigit() for c in compact):
                return []
        # Reject known label words (OCR grabbed a label as value)
        _name_reject_words = ("memberid", "member", "subscriber", "enrollee",
                              "provider", "authorization", "phone", "fax")
        if any(w.lower().rstrip(":,#") in _name_reject_words for w in name_parts):
            return []
        # Reject prose sentences (real names don't have these words)
        _name_prose = ("seriously", "risk", "health", "life", "explain",
                       "approval", "denial", "sincerely", "department",
                       "please", "contact", "regarding", "information",
                       "services", "coverage", "benefits", "medical",
                       "language", "people", "free", "provide", "address")
        if sum(1 for w in _name_prose if w in combined_lower) >= 2:
            return []
        # Reject if average word length > 8 (garbled OCR prose tends to have long "words")
        avg_word_len = sum(len(w) for w in name_parts) / len(name_parts)
        if avg_word_len > 8 and len(name_parts) > 3:
            return []
        return tokens

    if field_key == "provider_name":
        # Remove pure date/date-range tokens that often bleed into provider labels.
        filtered_tokens = [t for t in tokens if not _DATE_RE.match(t.text.strip())]
        if not filtered_tokens:
            return []
        combined = " ".join(t.text.strip() for t in filtered_tokens)
        combined_lower = combined.lower()
        compact = re.sub(r"[^A-Za-z0-9]", "", combined)
        if compact:
            digit_ratio = sum(1 for c in compact if c.isdigit()) / len(compact)
            if digit_ratio > 0.35:
                return []
        # Reject obvious label capture instead of value.
        if (
            combined.endswith(":")
            or "provider name" in combined_lower
            or "servicing provider" in combined_lower
            or "requesting provider" in combined_lower
        ):
            return []
        # Must have at least 1 word with 3+ chars (single-word providers like "ProMedica" are valid)
        words = [w for w in combined.split() if len(w) > 0]
        if len(words) < 1 or len(combined) < 3:
            return []
        # Long prose sentences are not provider names.
        if len(words) > 12:
            return []
        # Reject meaningless suffix-only captures (e.g., "LLC").
        suffix_only = {"llc", "inc", "corp", "co", "ltd", "pllc"}
        if all(w.lower().strip(".,") in suffix_only for w in words):
            return []
        # Reject letter closings, greetings, section headers, and prose
        _provider_reject = ("sincerely", "seriously", "dear", "regards", "attention",
                            "department", "yours truly", "location address",
                            "service location", "risk your", "physical health",
                            "coverage", "benefits", "eligibility",
                            "please contact", "free language",
                            "you can ask", "appeal", "submitted within",
                            "service authorization", "clinical peer", "reason for appealing")
        if any(w in combined_lower for w in _provider_reject):
            return []
        return filtered_tokens

    if field_key in ("provider_phone", "provider_fax"):
        # Take only the FIRST phone-like match (reject concatenated numbers)
        for t in tokens:
            text = t.text.strip()
            if _PHONE_RE.match(text):
                # If token has multiple numbers, take only the first
                # Phone numbers are max ~15 chars (xxx-xxx-xxxx or xxxxxxxxxx)
                if len(text) > 16:
                    # Extract first phone-like segment
                    phone_match = re.match(r"[\d\(\)\-\.\s]{7,15}", text)
                    if phone_match:
                        trimmed = phone_match.group().strip()
                        return [OcrTokenSimple(
                            text=trimmed, x0=t.x0, y0=t.y0,
                            x1=t.x1, y1=t.y1,
                            confidence=t.confidence, line_number=t.line_number,
                        )]
                return [t]
        return []  # Strict: no fallback

    # General max-value-length guard: reject values that are obviously too long
    # (indicates grabbing surrounding prose instead of the actual field value)
    _MAX_LENGTHS = {
        "patient_name": 60,
        "provider_name": 70,
        "provider_phone": 25,
        "provider_fax": 25,
        "provider_npi": 15,
    }
    max_len = _MAX_LENGTHS.get(field_key, 0)
    if max_len > 0:
        combined = " ".join(t.text.strip() for t in tokens)
        if len(combined) > max_len:
            # Truncate to first N tokens that fit within length
            truncated = []
            running = 0
            for t in tokens:
                if running + len(t.text.strip()) > max_len:
                    break
                truncated.append(t)
                running += len(t.text.strip()) + 1
            return truncated if truncated else []

    return tokens


def _extract_inline_value(
    label_tokens: list[OcrTokenSimple],
    canon_labels: list[str],
) -> str | None:
    """Extract value from within a label token when OCR groups 'Label: Value' together.

    PaddleOCR frequently produces single tokens like 'Member Name: Kimberly Crowley'
    or 'Member Number:10000641601'. Also handles separators |, # and merged tokens
    like 'DOB11/2/80' or 'MemberD0B:01/09/1962' (OCR char confusion).
    """
    last_tok_text = label_tokens[-1].text.strip()

    # Find the first separator: colon, pipe, or hash
    sep_idx = -1
    for sep in (":", "|", "#"):
        idx = last_tok_text.find(sep)
        if idx >= 0 and (sep_idx < 0 or idx < sep_idx):
            sep_idx = idx

    # No separator found — try label prefix match (e.g., "DOB11/2/80", "DateSpan08/25/2025")
    if sep_idx < 0:
        for canon in canon_labels:
            canon_clean = canon.rstrip(":").rstrip("#").strip()
            # Try exact and OCR-normalized match
            for prefix in (canon_clean, _ocr_normalize(canon_clean)):
                tok_check = last_tok_text if prefix == canon_clean else _ocr_normalize(last_tok_text)
                idx = tok_check.lower().find(prefix.lower())
                if idx >= 0:
                    after = last_tok_text[idx + len(prefix):].strip()
                    if after and (after[0].isdigit() or after[0] in "/-("):
                        return after
        return None

    value_part = last_tok_text[sep_idx + 1:].strip()

    # Reject empty or punctuation-only values (e.g., "Medicaid ID #:" → ":")
    if not value_part or not any(c.isalnum() for c in value_part):
        return None

    label_part = last_tok_text[:sep_idx].strip().lower()

    # If we matched multiple tokens, combine their text for the label check
    if len(label_tokens) > 1:
        combined = " ".join(t.text for t in label_tokens).strip()
        for sep in (":", "|", "#"):
            combined_idx = combined.find(sep)
            if combined_idx >= 0:
                value_part = combined[combined_idx + 1:].strip()
                label_part = combined[:combined_idx].strip().lower()
                break

    # Re-check after multi-token combine
    if not value_part or not any(c.isalnum() for c in value_part):
        return None

    # Verify the pre-separator text matches one of our label patterns
    label_part_clean = label_part.rstrip(":").rstrip("#").rstrip("|").strip()
    label_no_space = label_part_clean.replace(" ", "")
    label_norm = _ocr_normalize(label_no_space)
    matched_label = False
    for canon in canon_labels:
        canon_clean = canon.rstrip(":").rstrip("#").strip().lower()  # lowercase for case-insensitive compare
        canon_no_space = canon_clean.replace(" ", "")
        canon_norm = _ocr_normalize(canon_no_space)
        if (
            canon_clean in label_part_clean
            or label_part_clean in canon_clean
            or canon_no_space in label_no_space
            or label_no_space in canon_no_space
            or canon_norm in label_norm
            or label_norm in canon_norm
        ):
            matched_label = True
            break

    if not matched_label:
        return None

    return value_part


def _find_field_by_label(
    field_key: str,
    label_strings: list[str],
    tokens: list[OcrTokenSimple],
) -> ExtractionCandidate | None:
    """
    Find a field value by locating its label in OCR tokens.

    Strategy:
      1. Find tokens matching any of the label strings (multi-word aware)
      2. Collect value tokens to the RIGHT on the same line
      3. If no right-tokens, collect tokens BELOW the label
      4. Build ExtractionCandidate with evidence bbox and confidence

    Returns ExtractionCandidate or None if label not found.
    """
    if not tokens or not label_strings:
        return None

    canon_labels = [l.lower().strip() for l in label_strings]

    # --- Step 1: Find label tokens ---
    best_label_tokens: list[OcrTokenSimple] = []
    best_score = 0.0

    for label in canon_labels:
        label_words = label.split()

        for i, token in enumerate(tokens):
            tok_text = token.text.lower().strip()

            if len(label_words) == 1:
                # Single-word label: substring/exact match + OCR normalization
                word_clean = label_words[0].rstrip(":").rstrip("#")
                tok_clean = tok_text.rstrip(":").rstrip("#")
                tok_no_space = tok_clean.replace(" ", "")
                word_no_space = word_clean.replace(" ", "")
                tok_norm = _ocr_normalize(tok_no_space)
                word_norm = _ocr_normalize(word_no_space)
                if (word_clean in tok_clean or tok_clean in word_clean
                        or word_no_space in tok_no_space
                        or tok_no_space in word_no_space
                        or word_norm in tok_norm
                        or tok_norm in word_norm):
                    score = len(word_clean) / max(len(tok_clean), 1)
                    if score > best_score:
                        best_label_tokens = [token]
                        best_score = score
            else:
                # Multi-word: find tokens on same/adjacent lines
                matched = []
                words_found = 0
                for j in range(i, min(i + len(label_words) + 5, len(tokens))):
                    t = tokens[j]
                    if matched and abs(t.line_number - matched[0].line_number) > 1:
                        break
                    t_text = t.text.lower().strip().rstrip(":").rstrip("#")
                    t_norm = _ocr_normalize(t_text)
                    matched_any = False
                    for w in label_words[words_found:]:
                        w_clean = w.rstrip(":").rstrip("#")
                        w_norm = _ocr_normalize(w_clean)
                        if (w_clean in t_text or t_text in w_clean
                                or w_norm in t_norm or t_norm in w_norm):
                            if t not in matched:
                                matched.append(t)
                            words_found += 1
                            matched_any = True
                        else:
                            break
                    if not matched_any:
                        if matched:
                            continue  # Skip non-matching tokens after first match
                        break

                if words_found >= len(label_words) * 0.6:
                    score = words_found / len(label_words)
                    if score > best_score or (
                        score == best_score
                        and len(matched) < len(best_label_tokens)
                    ):
                        best_label_tokens = matched
                        best_score = score

    if not best_label_tokens:
        return None

    # --- Step 1b: Check for inline "Label: Value" in the label token ---
    # PaddleOCR often groups "Member Name: Kimberly Crowley" as one token.
    # If the matched token contains a colon followed by value text, extract it directly.
    inline_value = _extract_inline_value(best_label_tokens, canon_labels)
    if inline_value:
        # Validate inline value against field-type constraints
        # Build a synthetic token to run through the type filter
        last_tok = best_label_tokens[-1]
        synthetic = OcrTokenSimple(
            text=inline_value, x0=last_tok.x0, y0=last_tok.y0,
            x1=last_tok.x1, y1=last_tok.y1,
            confidence=last_tok.confidence, line_number=last_tok.line_number,
        )
        filtered = _filter_tokens_by_field_type(field_key, [synthetic])
        if filtered:
            # Use filtered token text (may be trimmed, e.g., date fields)
            clean_value = _strip_label_prefix(field_key, filtered[0].text.strip())
            clean_value = _normalize_date_value_for_field(field_key, clean_value)
            # Strip surrounding quotes
            if len(clean_value) >= 2 and clean_value[0] in ('"', "'") and clean_value[-1] in ('"', "'"):
                clean_value = clean_value[1:-1].strip()
            # Service code inline: stop at first non-code word
            # e.g. "G0480 DRUG TEST DEF 1-7 CLASSES" → "G0480"
            if field_key in ("service_code", "cpt_code", "hcpc_code") and clean_value:
                _sc_parts = []
                for _sc_word in clean_value.split():
                    _sc_clean = _sc_word.strip("()[]{}.,;:")
                    if re.match(r"^11[0-9]{4}$", _sc_clean):
                        _sc_clean = "H" + _sc_clean[2:]
                        _sc_word = _sc_clean
                    if any(c.isdigit() for c in _sc_clean) and re.match(r"^[A-Za-z0-9.\-]+$", _sc_clean) and len(_sc_clean) <= 10:
                        _sc_parts.append(_sc_word)
                    else:
                        break
                if _sc_parts:
                    clean_value = " ".join(_sc_parts)
            if clean_value:
                confidence = max(0.40, last_tok.confidence - OCR_LABEL_CONFIDENCE_PENALTY)
                evidence_bbox = {
                    "x0": last_tok.x0, "y0": last_tok.y0,
                    "x1": last_tok.x1, "y1": last_tok.y1,
                }
                # Sanity check
                roi_w = evidence_bbox["x1"] - evidence_bbox["x0"]
                roi_h = evidence_bbox["y1"] - evidence_bbox["y0"]
                if roi_w >= 0.005 and roi_h >= 0.003 and roi_w <= 0.9 and roi_h <= 0.3:
                    return ExtractionCandidate(
                        value=clean_value,
                        method=ExtractionMethodEnum.OCR_LABEL,
                        confidence=confidence,
                        evidence_bbox=evidence_bbox,
                        evidence_text=clean_value,
                    )

    # --- Step 2: Find value tokens to the RIGHT on the same line ---
    label_x0 = min(t.x0 for t in best_label_tokens)
    label_x1 = max(t.x1 for t in best_label_tokens)
    label_y0 = min(t.y0 for t in best_label_tokens)
    label_y1 = max(t.y1 for t in best_label_tokens)
    row_height = max(label_y1 - label_y0, 0.01)

    # Build sets of known label words and phrases to detect label boundaries
    _all_label_words = set()
    _all_label_phrases = set()
    for aliases in LABEL_ALIASES.values():
        for alias in aliases:
            phrase = alias.lower().rstrip(":").rstrip("#").strip()
            _all_label_phrases.add(phrase)
            for w in phrase.split():
                _all_label_words.add(w.rstrip(":").rstrip("#"))

    def _looks_like_label(text: str) -> bool:
        """Check if a token looks like a field label."""
        stripped = text.strip()
        if stripped.endswith(":"):
            return True
        clean = stripped.lower().rstrip(":").rstrip("#")
        if clean in _all_label_words and len(clean) > 2:
            return True
        if clean in _all_label_phrases:
            return True
        return False

    value_tokens: list[OcrTokenSimple] = []
    allow_pre_label_search = field_key in {"patient_name", "provider_name", "patient_dob"}

    # Search right first (label: value layout).
    right_candidates = []
    for token in tokens:
        if token in best_label_tokens:
            continue
        same_line = abs(token.y0 - label_y0) < row_height * 0.7
        to_right = token.x0 >= label_x1 - 0.01
        if same_line and to_right:
            right_candidates.append(token)

    right_candidates.sort(key=lambda t: t.x0)
    for i, tok in enumerate(right_candidates[:4]):
        if _looks_like_label(tok.text):
            break
        if i > 0 and tok.x0 - right_candidates[i - 1].x1 > 0.10:
            break
        value_tokens.append(tok)

    # Some forms OCR as "VALUE  Label:" on the same row; try left/overlap.
    if not value_tokens and allow_pre_label_search:
        left_candidates = []
        for token in tokens:
            if token in best_label_tokens:
                continue
            same_line = abs(token.y0 - label_y0) < row_height * 0.8
            left_or_overlap = token.x0 <= label_x0 + 0.02 and token.x1 <= label_x1 + 0.01
            if same_line and left_or_overlap:
                left_candidates.append(token)

        left_candidates.sort(key=lambda t: t.x1, reverse=True)
        picked_left: list[OcrTokenSimple] = []
        prev_x0 = None
        for tok in left_candidates:
            if _looks_like_label(tok.text):
                continue
            if prev_x0 is not None and prev_x0 - tok.x1 > 0.10:
                break
            picked_left.append(tok)
            prev_x0 = tok.x0
            if len(picked_left) >= 4:
                break
        if picked_left:
            value_tokens = list(reversed(picked_left))

    # Some forms place value above label; prefer closest row above before below.
    if not value_tokens and allow_pre_label_search:
        above_candidates = []
        above_min_y = max(0.0, label_y0 - max(row_height * 2.5, 0.06))
        col_x0 = label_x0 - 0.06
        col_x1 = label_x1 + 0.06
        for token in tokens:
            if token in best_label_tokens:
                continue
            above = above_min_y <= token.y1 <= label_y0 + 0.005
            h_close = token.x0 < col_x1 and token.x1 > col_x0
            if above and h_close:
                above_candidates.append(token)

        above_candidates.sort(key=lambda t: (t.y1, t.x0), reverse=True)
        if above_candidates:
            reference_y = None
            for tok in above_candidates:
                if _looks_like_label(tok.text):
                    continue
                reference_y = tok.y0
                break
            if reference_y is not None:
                same_row = [
                    tok for tok in above_candidates
                    if not _looks_like_label(tok.text)
                    and abs(tok.y0 - reference_y) <= max(row_height * 0.8, 0.012)
                ]
                same_row.sort(key=lambda t: t.x0)
                value_tokens = same_row[:4]

    # If still empty, try below.
    if not value_tokens:
        below_candidates = []
        below_max_y = label_y1 + max(row_height * 2.5, 0.06)
        col_x0 = label_x0 - 0.03
        col_x1 = label_x1 + 0.06
        for token in tokens:
            if token in best_label_tokens:
                continue
            below = label_y1 - 0.005 <= token.y0 <= below_max_y
            h_close = token.x0 < col_x1 and token.x1 > col_x0
            if below and h_close:
                below_candidates.append(token)

        below_candidates.sort(key=lambda t: (t.y0, t.x0))
        if below_candidates:
            first_value_line = None
            for tok in below_candidates:
                if not _looks_like_label(tok.text):
                    first_value_line = tok.line_number
                    break
            if first_value_line is not None:
                for tok in below_candidates:
                    if tok.line_number < first_value_line:
                        continue
                    if tok.line_number > first_value_line + 1:
                        break
                    if _looks_like_label(tok.text):
                        continue
                    value_tokens.append(tok)
                    if len(value_tokens) >= 4:
                        break

    if not value_tokens:
        return None

    # --- Step 3: Field-type-aware value cleaning ---
    value_tokens = _filter_tokens_by_field_type(field_key, value_tokens)
    if not value_tokens:
        return None

    value_text = _strip_label_prefix(field_key, " ".join(t.text for t in value_tokens).strip())
    value_text = _normalize_date_value_for_field(field_key, value_text)
    if not value_text:
        return None

    # --- Step 4: Value cleanup ---
    # Strip surrounding quotes
    if len(value_text) >= 2 and value_text[0] in ('"', "'") and value_text[-1] in ('"', "'"):
        value_text = value_text[1:-1].strip()
    # Deduplicate repeated values (e.g., "30 30" → "30" for units)
    if field_key == "units_requested":
        parts = value_text.split()
        if len(parts) >= 2 and len(set(parts)) == 1:
            value_text = parts[0]
    # Service code: stop at first non-code word (prevents "G0480 DRUG TEST DEF..." bleed)
    if field_key in ("service_code", "cpt_code", "hcpc_code"):
        parts = value_text.split()
        code_parts = []
        for part in parts:
            clean = part.strip("()[]{}.,;:")
            # OCR correction: "112036" → "H2036"
            if re.match(r"^11[0-9]{4}$", clean):
                clean = "H" + clean[2:]
                part = clean
            has_digit = any(c.isdigit() for c in clean)
            is_code_like = bool(re.match(r"^[A-Za-z0-9.\-]+$", clean)) and len(clean) <= 10
            if has_digit and is_code_like:
                code_parts.append(part)
            else:
                break
        if code_parts:
            value_text = " ".join(code_parts)
    if not value_text:
        return None

    # Confidence = average OCR confidence * penalty for being heuristic
    avg_conf = sum(t.confidence for t in value_tokens) / len(value_tokens)
    confidence = max(0.40, avg_conf - OCR_LABEL_CONFIDENCE_PENALTY)

    # Evidence bounding box (union of value tokens)
    evidence_bbox = {
        "x0": min(t.x0 for t in value_tokens),
        "y0": min(t.y0 for t in value_tokens),
        "x1": max(t.x1 for t in value_tokens),
        "y1": max(t.y1 for t in value_tokens),
    }

    # Sanity check
    roi_w = evidence_bbox["x1"] - evidence_bbox["x0"]
    roi_h = evidence_bbox["y1"] - evidence_bbox["y0"]
    if roi_w < 0.005 or roi_h < 0.003 or roi_w > 0.9 or roi_h > 0.3:
        return None

    return ExtractionCandidate(
        value=value_text,
        method=ExtractionMethodEnum.OCR_LABEL,  # OCR label-based extraction
        confidence=confidence,
        evidence_bbox=evidence_bbox,
        evidence_text=value_text,
    )
