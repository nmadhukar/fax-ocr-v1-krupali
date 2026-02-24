"""
LayoutLM Document QA client for field extraction.

Uses impira/layoutlm-document-qa — a LayoutLM model fine-tuned on
document question-answering tasks. For each field, we ask a targeted
question and the model extracts the answer directly from the document.

This is EXTRACTIVE (picks real text from the document), not generative,
so answers are always actual text that appears on the page.

Key advantages:
  - Works out-of-box (pre-trained on real documents)
  - 2-5 sec per question on CPU
  - Extractive = more accurate (answers are real document text)
  - Can be fine-tuned with review corrections via LoRA adapters

Model: impira/layoutlm-document-qa (~130 MB, Apache 2.0)
"""

import logging
import os
import time
from typing import Any

import numpy as np
from PIL import Image

from libs.shared.vlm.base import VlmClient, VlmConfig, VlmResponse

logger = logging.getLogger(__name__)

# Default model — can be overridden with a fine-tuned checkpoint
DEFAULT_MODEL = "impira/layoutlm-document-qa"

# Field → question mapping (single question — kept for backward compatibility).
FIELD_QUESTIONS = {
    "member_id":            "What is the member ID or Medicaid ID number?",
    "prior_auth_number":    "What is the prior authorization number or auth reference number?",
    "patient_name":         "What is the patient's full name (first and last name, not a code)?",
    "patient_dob":          "What is the patient's date of birth in MM/DD/YYYY format?",
    "auth_effective_date":  "What is the authorization start date or effective date?",
    "auth_expiration_date": "What is the authorization expiration date or end date?",
    "next_review_date":     "What is the next scheduled review date (not the auth start or end date)?",
    "provider_name":        "What is the servicing provider or facility name?",
    "provider_npi":         "What is the provider NPI (10-digit National Provider Identifier)?",
    "provider_phone":       "What is the provider phone number?",
    "provider_fax":         "What is the provider fax number?",
    "service_code":         "What is the HCPCS or CPT procedure service code (like H2036 or 97151)?",
    "units_requested":      "How many units of service are requested or authorized?",
    "diagnosis_code":       "What is the ICD-10 diagnosis code (like F84.0 or Z51.11, not a service code)?",
    "decision":             "What is the prior authorization decision (APPROVED, DENIED, or PENDING)?",
}

# Multiple question variants per field — asking the same thing in different ways
# activates different attention regions in LayoutLM, boosting recall.
# ─── IMPORTANT PROMPTING RULES ───────────────────────────────────────────────
# 1. patient_name  : explicitly exclude codes/numbers — the model confuses auth
#                    numbers (alphanumeric like "0806WD89S") with names.
# 2. diagnosis_code: explicitly distinguish from service/billing codes.
#                    ICD-10 starts with a letter (F, Z, G, M…) then digits+decimal.
#                    Service codes (HCPCS/CPT like H2036, 97151) are different.
# 3. prior_auth_number: give a format hint so model looks for alphanumeric string.
# 4. provider_npi  : always say "10-digit" to avoid picking up member_id.
# 5. next_review_date: distinguish from auth start/end dates.
# ─────────────────────────────────────────────────────────────────────────────
FIELD_QUESTIONS_VARIANTS: dict[str, list[str]] = {
    "member_id": [
        "What is the member ID or Medicaid ID number? (numeric digits only)",
        "What is the subscriber ID or insurance member number?",
        "What is the plan member identification number?",
    ],
    "prior_auth_number": [
        "What is the prior authorization number? (alphanumeric reference like 0806WD89S)",
        "What is the authorization number or case reference number?",
        "What is the PA number or auth reference code?",
    ],
    "patient_name": [
        "What is the patient's full name? (a person's first and last name, NOT a code or number)",
        "What is the member's full name? (should contain only letters, not digits or authorization codes)",
        "Who is the patient? Give their full name with first and last name.",
    ],
    "patient_dob": [
        "What is the patient's date of birth? (format MM/DD/YYYY)",
        "What is the member's birth date?",
        "What date was the patient born?",
    ],
    "auth_effective_date": [
        "What is the authorization start date or effective date? (format MM/DD/YYYY)",
        "When does the prior authorization begin or take effect?",
        "What is the first date of the authorized service period?",
    ],
    "auth_expiration_date": [
        "What is the authorization expiration date or end date? (format MM/DD/YYYY)",
        "When does the prior authorization expire or end?",
        "What is the last date of the authorized service period?",
    ],
    "next_review_date": [
        "What is the next clinical review date? (different from the authorization start or end dates)",
        "When is the next scheduled review for this authorization case?",
        "What is the review or re-evaluation due date?",
    ],
    "provider_name": [
        "What is the treating or servicing provider's name or facility name?",
        "What is the name of the provider or clinic that submitted this request?",
        "What is the rendering provider or group name?",
    ],
    "provider_npi": [
        "What is the provider NPI? (exactly 10 digits, National Provider Identifier)",
        "What is the 10-digit National Provider Identifier number for the provider?",
        "What is the NPI number for the servicing provider or facility?",
    ],
    "provider_phone": [
        "What is the provider's contact phone number? (format (XXX) XXX-XXXX)",
        "What is the treating provider office phone number?",
    ],
    "provider_fax": [
        "What is the provider's fax number? (format (XXX) XXX-XXXX)",
        "What is the fax number to return information to the provider?",
    ],
    "service_code": [
        "What is the HCPCS or CPT service code for the procedure requested? (format like H2036 or 97151)",
        "What is the billing procedure code or treatment service code?",
        "What is the procedure or treatment service code (not the diagnosis code)?",
    ],
    "units_requested": [
        "How many units of service are requested or authorized? (a number)",
        "What is the number of authorized or approved treatment units or visits?",
        "How many units or days were requested for this authorization?",
    ],
    "diagnosis_code": [
        "What is the ICD-10 diagnosis code? (format: letter then digits like F84.0 or Z51.11, NOT a service code like H2036)",
        "What is the medical diagnosis ICD-10 code? (starts with a letter like F, Z, G, or M followed by numbers)",
        "What is the patient's diagnostic ICD code identifying their medical condition?",
    ],
    "decision": [
        "What is the prior authorization decision? (APPROVED, DENIED, or PENDING)",
        "Was this prior authorization approved or denied?",
        "What is the determination or decision on this authorization request?",
    ],
}


class LayoutLMClient(VlmClient):
    """
    VLM client backed by LayoutLM Document QA model.

    Uses HuggingFace's document-question-answering pipeline.
    For each field, asks a targeted question and extracts the answer
    from the document image + OCR tokens.
    """

    def __init__(self, config: VlmConfig | None = None):
        if config is None:
            config = VlmConfig(model_name=DEFAULT_MODEL)
        super().__init__(config)
        self._pipeline: Any = None

    def _load_model(self) -> None:
        """Load the LayoutLM pipeline lazily."""
        if self._pipeline is not None:
            return

        from transformers import pipeline

        model_name = self.config.model_name
        logger.info("Loading LayoutLM model: %s", model_name)

        # Set cache dir to D: if available (C: may be low on space)
        cache_dir = None
        if os.path.exists("D:\\"):
            cache_dir = "D:\\.cache\\huggingface\\hub"
            os.makedirs(cache_dir, exist_ok=True)

        # Load base model, optionally with LoRA adapter
        adapter_path = getattr(self.config, "adapter_path", None)
        if adapter_path:
            try:
                from peft import PeftModel
                from transformers import AutoModelForDocumentQuestionAnswering

                base_model = AutoModelForDocumentQuestionAnswering.from_pretrained(
                    model_name, cache_dir=cache_dir
                )
                logger.info("Loading LoRA adapter from: %s", adapter_path)
                model = PeftModel.from_pretrained(base_model, adapter_path)
                model.eval()

                self._pipeline = pipeline(
                    "document-question-answering",
                    model=model,
                    cache_dir=cache_dir,
                )
                logger.info("LayoutLM model loaded with LoRA adapter")
                return
            except ImportError:
                logger.warning("peft not installed — loading base model without adapter")
            except Exception as e:
                logger.warning("Failed to load LoRA adapter: %s — using base model", e)

        self._pipeline = pipeline(
            "document-question-answering",
            model=model_name,
            cache_dir=cache_dir,
        )
        logger.info("LayoutLM model loaded on CPU")

    def extract_candidates(
        self,
        image: np.ndarray,
        field_keys: list[str] | None = None,
        word_boxes: list[tuple[str, list[int]]] | None = None,
        top_k: int = 3,
    ) -> dict[str, list[tuple[str, float]]]:
        """
        Extract field candidates using multiple question variants + top-K answers.

        For each field we ask all question variants and collect top_k answers
        from each call.  Results are deduplicated (case-insensitive) keeping
        the highest score per unique answer.  The caller picks the first
        candidate that passes field-type validation.

        Returns:
            dict mapping field_key → list[(answer, score)] sorted by score desc.
        """
        self._load_model()

        fields_to_extract = field_keys or list(FIELD_QUESTIONS_VARIANTS.keys())
        pil_image = _numpy_to_pil(image)
        all_candidates: dict[str, list[tuple[str, float]]] = {}

        for field_key in fields_to_extract:
            variants = FIELD_QUESTIONS_VARIANTS.get(
                field_key, [FIELD_QUESTIONS.get(field_key, "")]
            )
            variants = [q for q in variants if q]

            # answer_lower → (original_cleaned, best_score)
            seen: dict[str, tuple[str, float]] = {}

            for question in variants:
                try:
                    pipe_input: dict = {"image": pil_image, "question": question, "top_k": top_k}
                    if word_boxes:
                        pipe_input["word_boxes"] = word_boxes

                    results = self._pipeline(**pipe_input)
                    if not results:
                        continue
                    if isinstance(results, dict):
                        results = [results]

                    for result in results:
                        raw_ans = result.get("answer", "").strip()
                        score = float(result.get("score", 0.0))
                        if not raw_ans or score < 0.01:
                            continue
                        cleaned = _strip_label_prefix(raw_ans)
                        if not cleaned:
                            continue
                        key_lower = cleaned.lower()
                        existing_score = seen.get(key_lower, (None, -1.0))[1]
                        if score > existing_score:
                            seen[key_lower] = (cleaned, score)

                except Exception as e:
                    logger.debug("LayoutLM variant '%s' failed for %s: %s", question, field_key, e)

            if seen:
                sorted_cands = sorted(seen.values(), key=lambda x: x[1], reverse=True)
                all_candidates[field_key] = sorted_cands  # [(answer, score), ...]

        return all_candidates

    def extract(
        self,
        image: np.ndarray,
        field_keys: list[str] | None = None,
        word_boxes: list[tuple[str, list[int]]] | None = None,
    ) -> VlmResponse:
        """
        Extract fields by asking questions about the document image.

        Args:
            image: Page image as numpy array (BGR or grayscale).
            field_keys: Which fields to extract. If None, extracts all.
            word_boxes: Pre-computed OCR tokens as [(word, [x0, y0, x1, y1]), ...].
                       Coordinates must be in 0-1000 range (LayoutLM convention).
                       If None, the pipeline runs its own OCR (needs pytesseract).

        Returns:
            VlmResponse with extracted field values.
        """
        self._load_model()

        start = time.monotonic()

        # Delegate to extract_candidates() and take the top answer per field
        candidates = self.extract_candidates(
            image, field_keys=field_keys, word_boxes=word_boxes, top_k=3
        )
        extracted: dict[str, Any] = {
            field_key: cands[0][0]  # top answer string
            for field_key, cands in candidates.items()
            if cands
        }

        latency = (time.monotonic() - start) * 1000

        logger.info(
            "LayoutLM extraction: %d fields in %.0f ms",
            len(extracted), latency,
        )

        return VlmResponse(
            fields=extracted,
            raw_sequence=str(extracted),
            model=self.config.model_name,
            latency_ms=latency,
        )

    def extract_single_field(
        self,
        image: np.ndarray,
        question: str,
        word_boxes: list[tuple[str, list[int]]] | None = None,
    ) -> tuple[str, float]:
        """
        Extract a single field by asking one question.

        Returns (answer, confidence) tuple.
        """
        self._load_model()

        if len(image.shape) == 2:
            pil_image = Image.fromarray(image, mode="L").convert("RGB")
        elif image.shape[2] == 4:
            pil_image = Image.fromarray(image[:, :, :3][:, :, ::-1], mode="RGB")
        else:
            pil_image = Image.fromarray(image[:, :, ::-1], mode="RGB")

        pipe_input = {"image": pil_image, "question": question}
        if word_boxes:
            pipe_input["word_boxes"] = word_boxes

        results = self._pipeline(**pipe_input)

        if results and len(results) > 0:
            return results[0].get("answer", ""), results[0].get("score", 0.0)
        return "", 0.0

    def is_available(self) -> bool:
        """Check if LayoutLM can be loaded."""
        try:
            from transformers import pipeline as _  # noqa: F401
            return True
        except ImportError:
            return False


import re as _re


def _numpy_to_pil(image: np.ndarray) -> "Image.Image":
    """Convert numpy BGR/grayscale array to PIL RGB image."""
    if len(image.shape) == 2:
        return Image.fromarray(image, mode="L").convert("RGB")
    if image.shape[2] == 4:
        return Image.fromarray(image[:, :, :3][:, :, ::-1], mode="RGB")
    return Image.fromarray(image[:, :, ::-1], mode="RGB")

# Common label prefixes the model includes in answers
_LABEL_PREFIX_PATTERN = _re.compile(
    r"^(?:"
    r"member\s*(?:id|number|name|dob|date\s*of\s*birth|#)|"
    r"health\s*plan\s*(?:id|number)?|"
    r"medicaid\s*(?:id|number)?|"
    r"subscriber\s*(?:id|name|number)?|"
    r"enrollee\s*(?:id|name)?|"
    r"plan\s*(?:id|number)?|"
    r"authorization\s*(?:number|#|no\.?)|"
    r"auth(?:orization)?\s*(?:number|#|no\.?)|"
    r"prior\s*auth(?:orization)?\s*(?:number|#)?|"
    r"pa\s*(?:number|#)|"
    r"reference\s*(?:number|#)|"
    r"patient\s*(?:name|id|dob|date\s*of\s*birth)?|"
    r"name|"
    r"date\s*of\s*birth|"
    r"birth\s*date|"
    r"dob|"
    r"fax\s*(?:number|#)|"
    r"phone\s*(?:number|#)|"
    r"telephone\s*(?:number)?|"
    r"servicing\s*provider\s*(?:name)?|"
    r"requesting\s*provider\s*(?:name)?|"
    r"ordering\s*provider\s*(?:name)?|"
    r"attending\s*provider\s*(?:name)?|"
    r"treating\s*provider\s*(?:name)?|"
    r"rendering\s*provider\s*(?:name|group)?|"
    r"provider\s*(?:name|npi)?|"
    r"facility\s*(?:name)?|"
    r"npi\s*(?:number|#)?|"
    r"service\s*(?:code)?|"
    r"cpt\s*(?:code)?|"
    r"hcpcs?\s*(?:code)?|"
    r"procedure\s*(?:code)?|"
    r"diagnosis\s*(?:code)?|"
    r"icd(?:-?10)?\s*(?:code)?|"
    r"dx\s*(?:code)?|"
    r"units\s*(?:requested|approved|authorized)?|"
    r"number\s*of\s*units|"
    r"effective\s*date|"
    r"expiration\s*date|"
    r"start\s*date|"
    r"end\s*date|"
    r"authorization\s*dates?|"
    r"auth\s*dates?|"
    r"next\s*(?:clinical\s*)?review\s*date|"
    r"review\s*date|"
    r"date\s*(?:span)?|"
    r"date"
    r")\s*[:=\-]?\s*",
    _re.IGNORECASE,
)


def _strip_label_prefix(answer: str) -> str:
    """Strip common label prefixes from LayoutLM answers.

    The model often returns "Label: Value" format — we only want the value.
    Returns "" when the entire answer is a label with no value (caller should skip).
    """
    stripped = _LABEL_PREFIX_PATTERN.sub("", answer, count=1).strip()
    # Return stripped (may be "") — do NOT fall back to original when pattern consumed everything
    return stripped
