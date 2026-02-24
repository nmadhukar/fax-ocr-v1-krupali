"""
Unit tests for HITL (Human‑in‑the‑Loop) module.

Tests compute_field_flags and apply_human_corrections.
"""

import pytest

from libs.shared.extraction.hitl import (
    REASON_LOW_CONFIDENCE,
    REASON_MISSING_VALUE,
    apply_human_corrections,
    compute_field_flags,
)


class TestComputeFieldFlags:
    """Tests for compute_field_flags()."""

    def test_critical_field_below_threshold(self):
        extraction = {
            "patient_name": {
                "value": "John Doe",
                "confidence": 0.50,
                "method": "TEMPLATE_OCR",
            },
        }
        flags = compute_field_flags(extraction)
        assert len(flags) == 1
        flag = flags[0]
        assert flag["field_key"] == "patient_name"
        assert flag["reason"] == REASON_LOW_CONFIDENCE

    def test_non_critical_field_below_threshold(self):
        extraction = {
            "provider_name": {
                "value": "Dr. Smith",
                "confidence": 0.50,
                "method": "TEMPLATE_OCR",
            },
        }
        flags = compute_field_flags(extraction)
        assert len(flags) == 1
        assert flags[0]["field_key"] == "provider_name"

    def test_high_confidence_not_flagged(self):
        extraction = {
            "patient_name": {
                "value": "John Doe",
                "confidence": 0.95,
                "method": "TEMPLATE_OCR",
            },
        }
        flags = compute_field_flags(extraction)
        assert not flags

    def test_empty_value_flagged(self):
        extraction = {
            "patient_name": {
                "value": "",
                "confidence": 0.90,
                "method": "TEMPLATE_OCR",
            },
        }
        flags = compute_field_flags(extraction)
        assert len(flags) == 1
        assert flags[0]["reason"] == REASON_MISSING_VALUE

    def test_missing_required_field(self):
        extraction = {
            "patient_name": {
                "value": "John",
                "confidence": 0.95,
                "method": "TEMPLATE_OCR",
            },
        }
        flags = compute_field_flags(
            extraction,
            required_fields=["patient_name", "member_id"],
        )
        missing_flags = [f for f in flags if f["field_key"] == "member_id"]
        assert len(missing_flags) == 1
        assert missing_flags[0]["reason"] == REASON_MISSING_VALUE

    def test_human_review_skipped(self):
        """Fields already corrected by human review should not be flagged."""
        extraction = {
            "patient_name": {
                "value": "John Doe",
                "confidence": 0.50,
                "method": "HUMAN_REVIEW",
            },
        }
        flags = compute_field_flags(extraction)
        assert not flags

    def test_custom_thresholds(self):
        extraction = {
            "provider_name": {
                "value": "Dr. Smith",
                "confidence": 0.60,
                "method": "TEMPLATE_OCR",
            },
        }
        # With a higher default threshold, more fields get flagged
        flags = compute_field_flags(extraction, default_threshold=0.80)
        assert len(flags) == 1

        # With a lower threshold, it passes
        flags = compute_field_flags(extraction, default_threshold=0.50)
        assert not flags

    def test_sort_order_critical_first(self):
        extraction = {
            "provider_name": {
                "value": "Dr. Smith",
                "confidence": 0.50,
                "method": "TEMPLATE_OCR",
            },
            "patient_name": {
                "value": "John",
                "confidence": 0.50,
                "method": "TEMPLATE_OCR",
            },
        }
        flags = compute_field_flags(extraction)
        assert len(flags) == 2
        # Critical field (patient_name) should come first
        assert flags[0]["field_key"] == "patient_name"

    def test_empty_extraction(self):
        flags = compute_field_flags({})
        assert flags == []


class TestApplyHumanCorrections:
    """Tests for apply_human_corrections()."""

    def test_overwrite_existing_field(self):
        extraction = {
            "patient_name": {
                "value": "Jon Doe",
                "confidence": 0.80,
                "method": "TEMPLATE_OCR",
                "candidates": [],
            },
        }
        result = apply_human_corrections(extraction, {"patient_name": "John Doe"})
        assert result["patient_name"]["value"] == "John Doe"
        assert result["patient_name"]["confidence"] == 1.0
        assert result["patient_name"]["method"] == "HUMAN_REVIEW"

    def test_original_prediction_archived(self):
        extraction = {
            "patient_name": {
                "value": "Jon Doe",
                "confidence": 0.80,
                "method": "TEMPLATE_OCR",
                "candidates": [],
            },
        }
        result = apply_human_corrections(extraction, {"patient_name": "John Doe"})
        candidates = result["patient_name"]["candidates"]
        assert any(c.get("_superseded_by_human") for c in candidates)
        assert any(c["value"] == "Jon Doe" for c in candidates)

    def test_create_new_field(self):
        extraction = {}
        result = apply_human_corrections(extraction, {"member_id": "M12345"})
        assert result["member_id"]["value"] == "M12345"
        assert result["member_id"]["confidence"] == 1.0

    def test_does_not_mutate_original(self):
        extraction = {
            "patient_name": {
                "value": "Jon Doe",
                "confidence": 0.80,
                "method": "TEMPLATE_OCR",
                "candidates": [],
            },
        }
        apply_human_corrections(extraction, {"patient_name": "John Doe"})
        assert extraction["patient_name"]["value"] == "Jon Doe"
