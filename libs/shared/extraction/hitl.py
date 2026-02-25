"""
Human-in-the-Loop (HITL) review layer.

Computes per-field confidence flags during pipeline processing.
Flags are stored on fax_extraction.flagged_fields and surfaced
in the review packet so reviewers focus on low-confidence fields.

Human corrections are applied back to extraction_json with
method=HUMAN_REVIEW and confidence=1.0.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from libs.shared.extraction.constants import CRITICAL_FIELDS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-field confidence thresholds.
# Fields with confidence < threshold are flagged for human review.
# Critical clinical fields get a stricter threshold.
# ---------------------------------------------------------------------------

_CRITICAL_THRESHOLD = 0.85   # critical clinical fields
_DEFAULT_THRESHOLD = 0.75    # all other fields

# Critical fields imported from the single source of truth
_CRITICAL_FIELDS = CRITICAL_FIELDS

# Per-field threshold table (everything not listed uses _DEFAULT_THRESHOLD)
FIELD_REVIEW_THRESHOLDS: dict[str, float] = {
    field: _CRITICAL_THRESHOLD for field in _CRITICAL_FIELDS
}

# Reason codes stored in flagged_fields entries
REASON_LOW_CONFIDENCE = "LOW_CONFIDENCE"
REASON_MISSING_VALUE = "MISSING_VALUE"


def compute_field_flags(
    extraction_json: dict[str, Any],
    required_fields: list[str] | None = None,
    payer_name: str | None = None,
    default_threshold: float = _DEFAULT_THRESHOLD,
    critical_threshold: float = _CRITICAL_THRESHOLD,
) -> list[dict[str, Any]]:
    """
    Compute per-field HITL flags for an extraction result.

    A field is flagged when:
    - Its confidence falls below the per-field threshold (LOW_CONFIDENCE).
    - It is in `required_fields` but entirely absent from extraction_json (MISSING_VALUE).
    - It has a record in extraction_json but the value is empty (MISSING_VALUE).

    Args:
        extraction_json: Final merged extraction fields dict.
        required_fields: Optional list of field keys that must be present.
        payer_name: Detected payer name (logged for context; reserved for future rules).
        default_threshold: Override for non-critical field threshold.
        critical_threshold: Override for critical field threshold.

    Returns:
        List of flag dicts ordered by decreasing severity:
        [{"field_key": str, "reason": str, "threshold": float, "confidence": float | None}]
    """
    # Build a threshold lookup using caller-supplied overrides
    def _threshold(field_key: str) -> float:
        if field_key in _CRITICAL_FIELDS:
            return critical_threshold
        return FIELD_REVIEW_THRESHOLDS.get(field_key, default_threshold)

    flags: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    # 1. Scan existing fields for low confidence or empty values
    for field_key, field_data in extraction_json.items():
        seen_keys.add(field_key)
        if not isinstance(field_data, dict):
            continue

        confidence = field_data.get("confidence")
        value = field_data.get("value")

        # Skip fields already corrected by a human (authoritative, conf=1.0)
        if field_data.get("method") == "HUMAN_REVIEW":
            continue

        # Empty value despite having a record
        if value is None or (isinstance(value, str) and not value.strip()):
            flags.append({
                "field_key": field_key,
                "reason": REASON_MISSING_VALUE,
                "threshold": _threshold(field_key),
                "confidence": float(confidence) if confidence is not None else None,
            })
            continue

        # Low confidence
        if confidence is not None and float(confidence) < _threshold(field_key):
            flags.append({
                "field_key": field_key,
                "reason": REASON_LOW_CONFIDENCE,
                "threshold": _threshold(field_key),
                "confidence": float(confidence),
            })

    # 2. Required fields that are entirely absent
    if required_fields:
        for field_key in required_fields:
            if field_key not in seen_keys:
                flags.append({
                    "field_key": field_key,
                    "reason": REASON_MISSING_VALUE,
                    "threshold": _threshold(field_key),
                    "confidence": None,
                })

    # Sort: critical fields first, then by ascending confidence (worst first)
    def _sort_key(flag: dict[str, Any]) -> tuple[int, float]:
        is_critical = int(flag["field_key"] in _CRITICAL_FIELDS)
        conf = flag["confidence"] if flag["confidence"] is not None else -1.0
        return (-is_critical, conf)

    flags.sort(key=_sort_key)

    if flags:
        logger.info(
            "HITL: %d field(s) flagged%s",
            len(flags),
            f" [payer={payer_name}]" if payer_name else "",
        )

    return flags


def apply_human_corrections(
    extraction_json: dict[str, Any],
    corrections: dict[str, str],
) -> dict[str, Any]:
    """
    Apply human reviewer corrections to an extraction result.

    Each corrected field gets:
    - value = the reviewer's corrected value
    - confidence = 1.0  (human is authoritative)
    - method = HUMAN_REVIEW
    - candidates list preserved for full audit trail

    Args:
        extraction_json: Current extraction_json dict (not mutated).
        corrections: Mapping of {field_key: corrected_value} from the reviewer.

    Returns:
        New extraction_json dict with corrections applied.
    """
    updated = copy.deepcopy(extraction_json)

    for field_key, corrected_value in corrections.items():
        existing = updated.get(field_key)

        if isinstance(existing, dict):
            # Preserve existing candidates for the audit trail
            candidates = list(existing.get("candidates", []))

            # Archive the original machine prediction as a superseded candidate
            orig_value = existing.get("value")
            orig_conf = existing.get("confidence")
            if orig_value and orig_value != corrected_value:
                # Remove any previous HUMAN_REVIEW candidate to avoid duplication
                candidates = [c for c in candidates if c.get("method") != "HUMAN_REVIEW"]
                candidates.append({
                    "value": orig_value,
                    "method": existing.get("method", "UNKNOWN"),
                    "confidence": orig_conf,
                    "_superseded_by_human": True,
                })

            updated[field_key] = {
                "value": corrected_value,
                "confidence": 1.0,
                "method": "HUMAN_REVIEW",
                "evidence_bbox": existing.get("evidence_bbox"),
                "candidates": candidates,
            }
        else:
            # Field did not exist in original extraction — create from scratch
            updated[field_key] = {
                "value": corrected_value,
                "confidence": 1.0,
                "method": "HUMAN_REVIEW",
                "evidence_bbox": None,
                "candidates": [],
            }

        logger.info(
            "HITL correction applied: field=%s (conf=1.0, method=HUMAN_REVIEW)",
            field_key,
        )

    return updated
