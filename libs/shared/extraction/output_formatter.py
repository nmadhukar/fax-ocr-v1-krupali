"""
Client-facing output formatter for extraction results.

Converts the internal extraction_json (which stores full pipeline details —
candidates, evidence text, validation state) into a clean, flat structure
suitable for API consumers.

Internal DB format per field:
    {
        "value": "Jane Doe",
        "confidence": 0.95,
        "method": "HYBRID",
        "evidence_bbox": {...},
        "evidence_text": "...",
        "candidates": [...],
        "validation_passed": true,
        "validation_errors": [],
        "not_present": false
    }

Client output format per field:
    {
        "value": "Jane Doe",        # null when not found
        "confidence": 0.95,         # 0.0–1.0
        "source": "TEMPLATE_OCR",   # winning extraction method
        "not_present": false        # true = field not in this document
    }
"""

from __future__ import annotations

from typing import Any

# Human-readable method labels shown to clients
_METHOD_DISPLAY: dict[str, str] = {
    "TEMPLATE_OCR": "TEMPLATE_OCR",
    "OCR_LABEL":    "OCR_LABEL",
    "LAYOUTLM":     "LAYOUTLM",
    "VLM":          "VLM",
    "DONUT":        "DONUT",
    "HYBRID":       "HYBRID",
    "HUMAN_REVIEW": "HUMAN_REVIEW",
    "HUMAN":        "HUMAN",
    "LLM":          "LLM",
    "SYSTEM":       "SYSTEM",
}


def _safe_float(value: Any) -> float:
    """Safely convert a confidence value to a rounded float."""
    if value is None:
        return 0.0
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return 0.0


def format_field(field_data: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a single internal field dict to the client-facing format.

    Args:
        field_data: Internal extraction dict for one field.

    Returns:
        Clean dict with value, confidence, source, not_present.
    """
    value = field_data.get("value")
    confidence = field_data.get("confidence", 0.0)
    method = field_data.get("method", "UNKNOWN")
    not_present = field_data.get("not_present", False) or (
        value is None or (isinstance(value, str) and not value.strip())
    )

    return {
        "value": None if not_present else value,
        "confidence": _safe_float(confidence),
        "source": _METHOD_DISPLAY.get(method, method),
        "not_present": bool(not_present),
    }


def format_for_client(extraction_json: dict[str, Any]) -> dict[str, Any]:
    """
    Convert the full internal extraction_json to the client-facing format.

    Each field is reduced to {value, confidence, source, not_present}.
    Internal pipeline details (candidates, evidence_text, bbox, validation)
    are stripped — those stay in the DB for audit and review purposes.

    Args:
        extraction_json: The extraction_json dict from fax_extraction table.

    Returns:
        Clean dict mapping field_key → {value, confidence, source, not_present}.

    Example output:
        {
            "patient_name":         {"value": "Jane Doe",    "confidence": 0.95, "source": "TEMPLATE_OCR", "not_present": false},
            "member_id":            {"value": "MBR001234",   "confidence": 0.98, "source": "HYBRID",       "not_present": false},
            "prior_auth_number":    {"value": "PA-00123",    "confidence": 0.90, "source": "TEMPLATE_OCR", "not_present": false},
            "auth_effective_date":  {"value": "08/07/2025",  "confidence": 1.00, "source": "HYBRID",       "not_present": false},
            "auth_expiration_date": {"value": "09/05/2025",  "confidence": 1.00, "source": "HYBRID",       "not_present": false},
            "decision":             {"value": "APPROVED",    "confidence": 0.90, "source": "TEMPLATE_OCR", "not_present": false},
            "units_requested":      {"value": null,          "confidence": 0.00, "source": "HYBRID",       "not_present": true},
        }
    """
    result: dict[str, Any] = {}

    for field_key, field_data in extraction_json.items():
        if not isinstance(field_data, dict):
            # Bare scalar value (legacy or fallback) — wrap it
            result[field_key] = {
                "value": field_data if field_data is not None else None,
                "confidence": 0.0,
                "source": "UNKNOWN",
                "not_present": field_data is None,
            }
            continue

        result[field_key] = format_field(field_data)

    return result


def format_summary(
    extraction_json: dict[str, Any],
    flagged_fields: list[dict[str, Any]] | None = None,
    payer_name: str | None = None,
    fax_received_date: str | None = None,
) -> dict[str, Any]:
    """
    Full client response for a completed job — fields + quality summary.

    Args:
        extraction_json: Internal extraction_json from DB.
        flagged_fields: Optional HITL flag list from fax_extraction.flagged_fields.
        payer_name: Insurance company name from job metadata.
        fax_received_date: Formatted timestamp from job.created_at.

    Returns:
        Dict with fields, flagged_field_keys, quality stats.
    """
    fields = format_for_client(extraction_json)

    # Inject metadata-sourced fields
    if payer_name is not None:
        fields["payer_name"] = {
            "value": payer_name,
            "confidence": 1.0,
            "source": "SYSTEM",
            "not_present": False,
        }
    if fax_received_date is not None:
        fields["fax_received_date"] = {
            "value": fax_received_date,
            "confidence": 1.0,
            "source": "SYSTEM",
            "not_present": False,
        }

    total = len(fields)
    found = sum(1 for f in fields.values() if not f["not_present"])
    not_present_count = total - found
    flagged_keys = [f.get("field_key", "") for f in (flagged_fields or []) if isinstance(f, dict)]

    return {
        "fields": fields,
        "summary": {
            "total_fields": total,
            "fields_found": found,
            "fields_not_present": not_present_count,
            "fields_flagged_for_review": len(flagged_keys),
            "flagged_field_keys": flagged_keys,
        },
    }
