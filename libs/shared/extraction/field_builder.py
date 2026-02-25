"""
Field builder: merges multiple extraction candidates into one final value.

Scoring hierarchy (highest score wins):
  1. Template extraction with a format-valid value — highest base
  2. Agreement between multiple sources — strong bonus (0.20 per extra source)
  3. Conflict between sources — small penalty (0.10)
  4. LayoutLM Document QA with clean value — small bonus (+0.07 base, +0.12 fine-tuned)
  5. Template with contaminated / prose value — penalised instead of bonused

Pre-processing (preprocess_vlm_candidates) demotes VLM results that are clearly
wrong before scoring: date confusion, member-ID contamination, fax-header values.
"""

import json
import os
import re as _re
from collections import Counter
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from libs.shared.db.models.enums import ExtractionMethodEnum


@dataclass
class ExtractionCandidate:
    """A single extraction candidate."""

    value: str
    method: ExtractionMethodEnum
    confidence: float
    evidence_bbox: dict[str, float] | None = None
    evidence_text: str | None = None
    token_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "value": self.value,
            "method": self.method.value,
            "confidence": self.confidence,
            "evidence_bbox": self.evidence_bbox,
            "evidence_text": self.evidence_text,
        }


@dataclass
class ExtractedFieldData:
    """Final extracted field with all candidates."""

    field_key: str
    value: str | None
    confidence: float
    method: ExtractionMethodEnum
    evidence_bbox: dict[str, float] | None
    evidence_text: str | None
    candidates: list[ExtractionCandidate]
    validation_passed: bool | None = None
    validation_errors: list[str] = field(default_factory=list)
    # Set when no source found meaningful evidence for this field
    not_present: bool = False
    not_present_reason: str | None = None   # "no_candidates" | "low_confidence_all_sources"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for DB storage (full internal representation)."""
        d: dict[str, Any] = {
            "field_key": self.field_key,
            "value": self.value,
            "confidence": self.confidence,
            "method": self.method.value,
            "evidence_bbox": self.evidence_bbox,
            "evidence_text": self.evidence_text,
            "candidates": [c.to_dict() for c in self.candidates],
            "validation_passed": self.validation_passed,
            "validation_errors": self.validation_errors,
        }
        if self.not_present:
            d["not_present"] = True
            d["not_present_reason"] = self.not_present_reason
        return d


class CandidateRanker:
    """
    Lightweight learned ranker loaded from a JSON artifact.

    Model format:
      {
        "version": "2026-02-25",
        "global": {"method_bias": {"TEMPLATE_OCR": 0.03, ...}},
        "fields": {"patient_name": {"method_bias": {"LAYOUTLM": 0.04}}}
      }
    """

    def __init__(self, model_path: str):
        self.model_path = model_path
        self.model: dict[str, Any] = {}
        self.loaded = False
        self._load()

    def _load(self) -> None:
        path = Path(self.model_path)
        if not path.exists():
            return
        try:
            self.model = json.loads(path.read_text(encoding="utf-8"))
            self.loaded = True
        except Exception:
            self.model = {}
            self.loaded = False

    def score_delta(
        self,
        *,
        field_key: str,
        candidate: ExtractionCandidate,
        candidates: list[ExtractionCandidate],
    ) -> float:
        if not self.loaded:
            return 0.0

        method_name = candidate.method.value
        delta = 0.0

        global_bias = (self.model.get("global") or {}).get("method_bias", {})
        field_bias = ((self.model.get("fields") or {}).get(field_key, {}) or {}).get(
            "method_bias",
            {},
        )
        delta += float(global_bias.get(method_name, 0.0))
        delta += float(field_bias.get(method_name, 0.0))

        # Reward repeated agreement among candidates (learned consensus prior)
        norm = (candidate.value or "").strip().lower()
        if norm:
            agree_count = sum(
                1 for c in candidates if (c.value or "").strip().lower() == norm
            )
            if agree_count > 1:
                delta += min(0.06, 0.03 * (agree_count - 1))

        return max(-0.20, min(0.20, delta))


class FieldBuilder:
    """
    Builds final field values from multiple extraction candidates.

    Scoring hierarchy (highest score wins):
      - Template extraction with a clean, format-valid value → highest base
      - Agreement between multiple sources → strong bonus (0.20 per extra source)
      - Conflict between sources → penalty (0.10 each)
      - LayoutLM with clean value → +0.07 bonus
      - Template with contaminated/prose value → penalty instead of bonus
    """

    def __init__(
        self,
        template_bonus: float = 0.10,
        agreement_bonus: float = 0.20,   # Raised: 2 sources agree = +0.20
        conflict_penalty: float = 0.10,
        vlm_multiplier: float = 0.70,    # 0.70 = base model (+0.07), 1.20 = fine-tuned (+0.12)
        ranker_enabled: bool = True,
        ranker_model_path: str | None = None,
    ):
        self.template_bonus = template_bonus
        self.agreement_bonus = agreement_bonus
        self.conflict_penalty = conflict_penalty
        self.vlm_multiplier = vlm_multiplier
        model_path = (
            ranker_model_path
            or os.environ.get("ADAPTIVE_CANDIDATE_RANKER_MODEL_PATH")
            or "models/candidate_ranker/model.json"
        )
        self.ranker = CandidateRanker(model_path) if ranker_enabled else None

    # Fields with confidence below this threshold are considered not present in the document.
    # Below 0.30 across all sources means no source found meaningful evidence.
    NOT_PRESENT_THRESHOLD: float = 0.30

    def build_field(
        self,
        field_key: str,
        candidates: list[ExtractionCandidate],
    ) -> ExtractedFieldData:
        """Build the final field value from all extraction candidates.

        Returns NOT_PRESENT (value=None) when no candidate exceeds
        NOT_PRESENT_THRESHOLD — meaning the field does not appear in this document.
        """
        if not candidates:
            return ExtractedFieldData(
                field_key=field_key,
                value=None,
                confidence=0.0,
                method=ExtractionMethodEnum.HYBRID,
                evidence_bbox=None,
                evidence_text=None,
                candidates=[],
                not_present=True,
                not_present_reason="no_candidates",
            )

        scored = self._score_candidates(candidates, field_key=field_key)
        best_candidate, score = max(scored, key=lambda x: x[1])
        final_conf = max(0.0, min(score, 1.0))

        method = (
            ExtractionMethodEnum.HYBRID if len(candidates) > 1 else best_candidate.method
        )

        # If the best score is below the meaningful-evidence threshold, or the
        # winning value is empty, the field is effectively not present in the document.
        value = best_candidate.value
        if final_conf < self.NOT_PRESENT_THRESHOLD or not (value or "").strip():
            return ExtractedFieldData(
                field_key=field_key,
                value=None,
                confidence=final_conf,
                method=method,
                evidence_bbox=None,
                evidence_text=None,
                candidates=candidates,
                not_present=True,
                not_present_reason="low_confidence_all_sources",
            )

        return ExtractedFieldData(
            field_key=field_key,
            value=value,
            confidence=final_conf,
            method=method,
            evidence_bbox=best_candidate.evidence_bbox,
            evidence_text=best_candidate.evidence_text,
            candidates=candidates,
        )

    def _score_candidates(
        self,
        candidates: list[ExtractionCandidate],
        field_key: str = "",
    ) -> list[tuple[ExtractionCandidate, float]]:
        """
        Score all candidates.

        Scoring rules:
          - Base: candidate.confidence
          - Template clean:  +template_bonus (0.10)
          - Template dirty:  -contamination_penalty (0.0–0.50)
          - LayoutLM:        +template_bonus * 0.70 (0.07)
          - Agreement (N>1): +agreement_bonus * (N-1)  → strong boost
          - Conflict:        -conflict_penalty (0.10)
        """
        # Group values for agreement detection (case-insensitive)
        value_groups: dict[str, list[ExtractionCandidate]] = {}
        for c in candidates:
            key = c.value.lower().strip() if c.value else ""
            value_groups.setdefault(key, []).append(c)

        scored: list[tuple[ExtractionCandidate, float]] = []

        for candidate in candidates:
            score = candidate.confidence

            # ── Method-specific adjustment ──────────────────────────
            if candidate.method == ExtractionMethodEnum.TEMPLATE_OCR:
                effective_key = field_key or (
                    candidate.field_key if hasattr(candidate, "field_key") else ""
                )
                contamination = _label_contamination_penalty(
                    effective_key, candidate.value or ""
                )
                if contamination == 0.0:
                    score += self.template_bonus          # clean template value
                else:
                    score -= contamination                 # dirty → penalise

            elif candidate.method in (
                ExtractionMethodEnum.LAYOUTLM,
                ExtractionMethodEnum.VLM,
                ExtractionMethodEnum.LLM,
            ):
                # VLM/LayoutLM gets a small bonus (less than template).
                # When they agree with template, the combined score is high.
                # Template alone beats a lone VLM on format-valid values.
                score += self.template_bonus * self.vlm_multiplier   # +0.07 base, +0.12 fine-tuned

            # ── Agreement / conflict adjustment ─────────────────────
            value_key = candidate.value.lower().strip() if candidate.value else ""
            agreeing = value_groups.get(value_key, [])

            if len(agreeing) > 1 and value_key:
                # Multiple methods agree on a non-empty value → strong confidence boost
                # Cap extra bonus so we don't exceed 1.0 before min()
                extra = min(self.agreement_bonus * (len(agreeing) - 1), 0.40)
                score += extra
            elif len(candidates) > 1:
                # Different sources disagree → small penalty
                score -= self.conflict_penalty

            if self.ranker is not None:
                score += self.ranker.score_delta(
                    field_key=field_key,
                    candidate=candidate,
                    candidates=candidates,
                )

            scored.append((candidate, score))

        return scored

    def check_agreement(
        self,
        candidates: list[ExtractionCandidate],
        normalize: bool = True,
    ) -> bool:
        """Return True if all candidates agree on the same value."""
        if len(candidates) <= 1:
            return True
        values = {
            (c.value or "").lower().strip() if normalize else (c.value or "")
            for c in candidates
        }
        return len(values) == 1

    def merge_evidence(
        self,
        candidates: list[ExtractionCandidate],
    ) -> tuple[dict[str, float] | None, str | None]:
        """Merge evidence from multiple candidates."""
        bboxes = [c.evidence_bbox for c in candidates if c.evidence_bbox]
        texts = [c.evidence_text for c in candidates if c.evidence_text]

        merged_bbox = None
        if bboxes:
            from libs.shared.utils.bbox_utils import compute_union_bbox
            merged_bbox = compute_union_bbox(bboxes)

        merged_text = " | ".join(set(texts)) if texts else None
        return merged_bbox, merged_text


# ── Pre-processing: VLM candidate intelligence ──────────────────────────────

# Date fields where VLM often returns the same confused date value
_DATE_FIELDS = frozenset({
    "patient_dob",
    "auth_effective_date",
    "auth_expiration_date",
    "next_review_date",
    "service_start_date",
    "service_end_date",
})

# VLM methods — all model-based extractors that are not template/OCR
_VLM_METHODS = frozenset({
    ExtractionMethodEnum.LAYOUTLM,
    ExtractionMethodEnum.VLM,
})

# Fax header MSG# pattern: e.g. "1963313490-008-1", "1846555098-008-1"
_FAX_HEADER_PAT = _re.compile(r"^\d{7,}-\d{3}-\d+$")


def preprocess_vlm_candidates(
    candidates_by_field: dict[str, list[ExtractionCandidate]],
) -> dict[str, list[ExtractionCandidate]]:
    """
    Intelligently pre-screen VLM/LayoutLM candidates BEFORE scoring.

    Detects and demotes (sets confidence → 0.10) candidates that are clearly wrong:

    1. Date confusion: if the same date value appears from VLM across 3+ different
       date fields, VLM is confusing a prominent page date (e.g. fax date, DOB) with
       field-specific dates.  Demote all those VLM date candidates → template/OCR wins.

    2. units_requested contamination: if a VLM candidate for units_requested is a
       long numeric string (≥6 digits) it is almost certainly the member_id copied
       from the form.  Demote it.

    3. Fax header in auth/member fields: MSG# values like "1963313490-008-1" are
       fax transmission headers, not field values.  Demote them.

    Returns a new dict (same structure, possibly modified confidences).
    """
    # ── 1. Detect VLM date confusion ────────────────────────────────────────
    vlm_date_counter: Counter = Counter()
    for fk in _DATE_FIELDS:
        for cand in candidates_by_field.get(fk, []):
            if cand.method in _VLM_METHODS and cand.value:
                vlm_date_counter[cand.value.strip()] += 1

    # A date is "confused" if VLM assigned it to 3+ different date fields
    confused_dates = {v for v, cnt in vlm_date_counter.items() if cnt >= 3}

    # ── 2. Build corrected candidate dict ───────────────────────────────────
    result: dict[str, list[ExtractionCandidate]] = {}

    for field_key, candidates in candidates_by_field.items():
        new_candidates: list[ExtractionCandidate] = []
        for cand in candidates:
            if cand.method not in _VLM_METHODS or not cand.value:
                new_candidates.append(cand)
                continue

            val = cand.value.strip()
            demote_reason = None

            # Check 1: confused date
            if field_key in _DATE_FIELDS and val in confused_dates:
                demote_reason = "vlm_date_confusion"

            # Check 2: units_requested is a long number (member_id contamination)
            # or contains a year pattern (e.g. "9 2023" from VLM confusion)
            elif field_key == "units_requested":
                digits_only = _re.sub(r"\D", "", val)
                if len(digits_only) >= 6:
                    demote_reason = "units_is_member_id"
                elif _re.search(r"\b20[12]\d\b", val):
                    demote_reason = "units_contains_year"

            # Check 3: fax header MSG# in auth/member fields
            elif field_key in ("prior_auth_number", "member_id"):
                if _FAX_HEADER_PAT.match(val):
                    demote_reason = "fax_header_value"

            if demote_reason:
                demoted = copy(cand)
                demoted.confidence = 0.10   # very low — let template/OCR win
                new_candidates.append(demoted)
            else:
                new_candidates.append(cand)

        result[field_key] = new_candidates

    return result


# ── Helpers: contamination detection ────────────────────────────────────────

# Words that indicate a template value captured label text instead of a value
_LABEL_INDICATOR_WORDS = frozenset({
    "member", "provider", "authorization", "patient", "subscriber",
    "servicing", "requesting", "facility", "name", "date", "birth",
    "span", "effective", "expiration", "review", "number",
})

# Common English stop words — prose phrases contain several of these
_PROSE_STOP_WORDS = frozenset({
    "and", "not", "the", "of", "in", "is", "are", "for",
    "that", "this", "with", "from", "have", "been", "will",
    "does", "may", "per", "any", "its", "was", "our", "by",
    "at", "or", "to", "a", "an", "no", "do",
})

# Maximum word counts per field type — values longer than this are prose/label bleed
_FIELD_MAX_WORDS: dict[str, int] = {
    "member_id": 2,
    "prior_auth_number": 3,
    "patient_dob": 2,
    "auth_effective_date": 2,
    "auth_expiration_date": 2,
    "next_review_date": 2,
    "service_code": 3,
    "provider_npi": 1,
    "units_requested": 3,
    "diagnosis_codes": 3,
}


def _label_contamination_penalty(field_key: str, value: str) -> float:
    """
    Return a score penalty (0.0–0.50) when a template candidate looks wrong.

    Failure modes detected:
      - units_requested is a long numeric string (member_id contamination)
      - Value is a fax header MSG# for auth/member fields
      - Value contains too many words for its field type (prose/label bleed)
      - Value contains prose stop-words (English sentence, not a field value)
      - Value starts with a label indicator word followed by colon (label captured)
      - Value is empty / placeholder
    """
    if not value:
        return 0.25  # empty → template extraction failed

    key = field_key.lower()
    words = value.split()
    word_count = len(words)

    # ── Field-specific checks (highest priority) ────────────────────────────

    # units_requested: must be a small integer (1–999), not a member ID
    if key == "units_requested":
        digits_only = _re.sub(r"\D", "", value)
        if digits_only and len(digits_only) >= 6:
            return 0.50  # member_id contamination
        if not _re.match(r"^\d{1,3}(\s*(visit|unit|day|session)s?)?$", value.strip(), _re.I):
            if not _re.match(r"^\d+$", value.strip()):
                return 0.25  # not a valid unit count
        return 0.0  # valid units value → no penalty

    # Fax header MSG# for auth/member fields: "1963313490-008-1"
    if key in ("prior_auth_number", "member_id"):
        if _FAX_HEADER_PAT.match(value.strip()):
            return 0.45

    # ── Too many words for a constrained field ──────────────────────────────
    max_words = _FIELD_MAX_WORDS.get(key)
    if max_words and word_count > max_words:
        # 0.10 per excess word, capped at 0.40
        return min(0.40, 0.10 * (word_count - max_words))

    # ── Prose detection: multiple common stop words → English sentence ───────
    if word_count >= 4:
        stop_count = sum(
            1 for w in words if w.lower().rstrip(".,;:") in _PROSE_STOP_WORDS
        )
        if stop_count >= 2:
            return min(0.40, 0.10 * stop_count)

    # ── Label indicator word at start with colon ────────────────────────────
    if words:
        first = words[0].lower().rstrip(":.,")
        if first in _LABEL_INDICATOR_WORDS and word_count >= 2:
            if _re.search(r"[:=]", value):
                return 0.20

    # ── Empty placeholder ────────────────────────────────────────────────────
    if value.strip() in ("", ".", ":", "-"):
        return 0.25

    return 0.0
