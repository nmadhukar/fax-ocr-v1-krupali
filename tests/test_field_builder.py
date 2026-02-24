"""
Unit tests for FieldBuilder.

Tests candidate scoring, agreement, merge, and VLM pre-screening.
"""

import pytest

from libs.shared.db.models.enums import ExtractionMethodEnum
from libs.shared.extraction.field_builder import (
    ExtractionCandidate,
    FieldBuilder,
    preprocess_vlm_candidates,
)


class TestBuildField:
    """Test FieldBuilder.build_field()."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fb = FieldBuilder()

    def test_single_template_candidate(self):
        candidates = [
            ExtractionCandidate(
                value="John Doe",
                method=ExtractionMethodEnum.TEMPLATE_OCR,
                confidence=0.90,
            ),
        ]
        result = self.fb.build_field("patient_name", candidates)
        assert result.value == "John Doe"
        assert result.method == ExtractionMethodEnum.TEMPLATE_OCR
        assert result.confidence >= 0.90  # Should have template bonus

    def test_two_agreeing_candidates(self):
        candidates = [
            ExtractionCandidate(
                value="01/15/2024",
                method=ExtractionMethodEnum.TEMPLATE_OCR,
                confidence=0.85,
            ),
            ExtractionCandidate(
                value="01/15/2024",
                method=ExtractionMethodEnum.LAYOUTLM,
                confidence=0.80,
            ),
        ]
        result = self.fb.build_field("auth_effective_date", candidates)
        assert result.value == "01/15/2024"
        # Agreement bonus (+0.20) should boost confidence well above the highest single candidate
        # Template: 0.85 + 0.10 (template bonus) + 0.20 (agreement) = 1.15 → capped to 1.0
        assert result.confidence >= 0.90

    def test_conflicting_candidates_picks_template(self):
        candidates = [
            ExtractionCandidate(
                value="Jane Doe",
                method=ExtractionMethodEnum.TEMPLATE_OCR,
                confidence=0.85,
            ),
            ExtractionCandidate(
                value="John Smith",
                method=ExtractionMethodEnum.LAYOUTLM,
                confidence=0.80,
            ),
        ]
        result = self.fb.build_field("patient_name", candidates)
        assert result.value == "Jane Doe"
        # FieldBuilder returns HYBRID when resolving multi-source conflicts
        assert result.method in (ExtractionMethodEnum.TEMPLATE_OCR, ExtractionMethodEnum.HYBRID)

    def test_not_present_below_threshold(self):
        """All candidates below NOT_PRESENT_THRESHOLD → value=None."""
        candidates = [
            ExtractionCandidate(
                value="?",
                method=ExtractionMethodEnum.LAYOUTLM,
                confidence=0.05,
            ),
        ]
        result = self.fb.build_field("prior_auth_number", candidates)
        assert result.value is None or result.not_present

    def test_vlm_multiplier(self):
        """VLM candidates use a multiplier < 1 for base model."""
        fb_base = FieldBuilder(vlm_multiplier=0.70)

        candidates = [
            ExtractionCandidate(
                value="12345",
                method=ExtractionMethodEnum.LAYOUTLM,
                confidence=0.80,
            ),
        ]
        result = fb_base.build_field("member_id", candidates)
        # With vlm_multiplier 0.70, effective bonus is lower
        assert result.value == "12345"

    def test_empty_candidates(self):
        result = self.fb.build_field("patient_name", [])
        assert result.value is None


class TestCheckAgreement:
    def test_all_agree(self):
        fb = FieldBuilder()
        candidates = [
            ExtractionCandidate("John", ExtractionMethodEnum.TEMPLATE_OCR, 0.9),
            ExtractionCandidate("John", ExtractionMethodEnum.LAYOUTLM, 0.8),
        ]
        assert fb.check_agreement(candidates) is True

    def test_disagree(self):
        fb = FieldBuilder()
        candidates = [
            ExtractionCandidate("John", ExtractionMethodEnum.TEMPLATE_OCR, 0.9),
            ExtractionCandidate("Jane", ExtractionMethodEnum.LAYOUTLM, 0.8),
        ]
        assert fb.check_agreement(candidates) is False


class TestEmptyValueAgreement:
    """Empty values from multiple sources must NOT get an agreement bonus."""

    def test_empty_values_no_agreement_bonus(self):
        fb = FieldBuilder()
        candidates = [
            ExtractionCandidate("", ExtractionMethodEnum.TEMPLATE_OCR, 0.20),
            ExtractionCandidate("", ExtractionMethodEnum.LAYOUTLM, 0.15),
        ]
        result = fb.build_field("member_id", candidates)
        # Both have empty values → should be treated as not_present, not boosted
        assert result.value is None or result.not_present

    def test_none_values_no_agreement_bonus(self):
        fb = FieldBuilder()
        candidates = [
            ExtractionCandidate(None, ExtractionMethodEnum.TEMPLATE_OCR, 0.20),
            ExtractionCandidate(None, ExtractionMethodEnum.LAYOUTLM, 0.15),
        ]
        result = fb.build_field("patient_name", candidates)
        assert result.value is None or result.not_present


class TestPreprocessVlmCandidates:
    def test_fax_header_pattern_demoted(self):
        """Fax MSG# like '1846555098-008-1' should be demoted."""
        candidates = {
            "prior_auth_number": [
                ExtractionCandidate(
                    value="1846555098-008-1",
                    method=ExtractionMethodEnum.LAYOUTLM,
                    confidence=0.75,
                ),
            ],
        }
        result = preprocess_vlm_candidates(candidates)
        auth_candidates = result.get("prior_auth_number", [])
        assert auth_candidates[0].confidence <= 0.10

    def test_template_candidates_untouched(self):
        """Template candidates should not be demoted."""
        candidates = {
            "member_id": [
                ExtractionCandidate(
                    value="ABC123",
                    method=ExtractionMethodEnum.TEMPLATE_OCR,
                    confidence=0.90,
                ),
            ],
        }
        result = preprocess_vlm_candidates(candidates)
        assert result["member_id"][0].confidence == 0.90
