"""
Production-grade confidence scoring for extracted fields.

Replaces the naive arithmetic mean with a weighted scoring system that
accounts for critical field importance, validation status, source agreement,
and payer-specific thresholds.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from libs.shared.config.payer_rules import PayerRulesLoader, get_payer_rules
from libs.shared.extraction.canonicalizer import FieldCanonicalizer
from libs.shared.extraction.constants import CRITICAL_FIELDS

logger = logging.getLogger(__name__)

# Critical fields imported from the single source of truth
DEFAULT_CRITICAL_FIELDS = list(CRITICAL_FIELDS)

# Weight multipliers
CRITICAL_FIELD_WEIGHT = 2.0
NORMAL_FIELD_WEIGHT = 1.0
MISSING_CRITICAL_PENALTY = 0.05      # Reduced from 0.15 — less harsh on missing fields
VALIDATION_FAILURE_PENALTY = 0.05    # Reduced from 0.10 — minor validation issues shouldn't crush score
AGREEMENT_BONUS = 0.10               # Increased from 0.05 — reward multi-source agreement more

# OCR quality thresholds
OCR_QUALITY_BLUR_THRESHOLD = 100.0   # Below = blurry, penalize
OCR_QUALITY_MIN_TEXT_DENSITY = 0.02  # Below = sparse, penalize
OCR_QUALITY_MAX_PENALTY = 0.08       # Reduced from 0.15 — clean faxes shouldn't get large penalties


@dataclass
class FieldScore:
    """Score breakdown for a single field."""

    field_key: str
    raw_confidence: float
    weighted_confidence: float
    weight: float
    is_critical: bool
    validation_passed: bool | None
    source_count: int
    sources_agree: bool


@dataclass
class ScoringResult:
    """Complete scoring result for a fax job."""

    overall_confidence: float
    field_scores: dict[str, FieldScore] = field(default_factory=dict)
    missing_critical_count: int = 0
    validation_failure_count: int = 0
    agreement_count: int = 0
    total_fields: int = 0
    critical_fields_complete: bool = True
    review_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "overall_confidence": round(self.overall_confidence, 4),
            "missing_critical_count": self.missing_critical_count,
            "validation_failure_count": self.validation_failure_count,
            "agreement_count": self.agreement_count,
            "total_fields": self.total_fields,
            "critical_fields_complete": self.critical_fields_complete,
            "field_scores": {
                k: {
                    "raw_confidence": round(v.raw_confidence, 4),
                    "weighted_confidence": round(v.weighted_confidence, 4),
                    "weight": v.weight,
                    "is_critical": v.is_critical,
                    "validation_passed": v.validation_passed,
                    "source_count": v.source_count,
                    "sources_agree": v.sources_agree,
                }
                for k, v in self.field_scores.items()
            },
        }


class ConfidenceScorer:
    """
    Production confidence scorer with weighted field scoring.

    Scoring factors:
    1. Per-field confidence (from extraction methods)
    2. Critical field weighting (2x for member_id, prior_auth_number, etc.)
    3. Missing required field penalties
    4. Validation failure penalties
    5. Multi-source agreement bonuses
    6. Payer-specific threshold enforcement
    """

    def __init__(self, payer_rules: PayerRulesLoader | None = None):
        """Initialize scorer with payer rules."""
        self.payer_rules = payer_rules or get_payer_rules()

    def score(
        self,
        fields: dict[str, dict[str, Any]],
        payer_name: str | None = None,
        candidates_by_field: dict[str, list[dict[str, Any]]] | None = None,
        ocr_quality: dict[str, float] | None = None,
    ) -> ScoringResult:
        """
        Calculate weighted confidence score for extracted fields.

        Args:
            fields: Merged extraction results {field_key: {value, confidence, method, ...}}.
            payer_name: Detected payer name for payer-specific rules.
            candidates_by_field: All candidates per field for agreement analysis.
            ocr_quality: Optional OCR quality metrics with keys:
                - avg_blur_score: Average Laplacian variance across pages (higher = sharper)
                - avg_text_density: Average text pixel ratio (0.0-1.0)
                - avg_token_confidence: Average OCR token confidence (0.0-1.0)
                Used to apply a quality-based penalty to overall confidence.

        Returns:
            ScoringResult with overall confidence and per-field breakdown.
        """
        result = ScoringResult(overall_confidence=0.0)

        # Get critical fields for this payer
        critical_fields = self._get_critical_fields(payer_name)

        # Score each extracted field
        weighted_sum = 0.0
        weight_total = 0.0

        for field_key, field_data in fields.items():
            if not isinstance(field_data, dict):
                continue

            value = field_data.get("value")
            if value is None:
                continue

            is_critical = field_key in critical_fields
            weight = CRITICAL_FIELD_WEIGHT if is_critical else NORMAL_FIELD_WEIGHT
            raw_conf = float(field_data.get("confidence", 0.0))

            # Adjust confidence based on validation
            adjusted_conf = raw_conf
            validation_passed = field_data.get("validation_passed")
            if validation_passed is False:
                adjusted_conf = max(0.0, adjusted_conf - VALIDATION_FAILURE_PENALTY)
                result.validation_failure_count += 1

            # Check source agreement
            source_count = 1
            sources_agree = True
            if candidates_by_field and field_key in candidates_by_field:
                candidates = candidates_by_field[field_key]
                source_count = len(candidates)
                if source_count > 1:
                    values = set()
                    for c in candidates:
                        v = (c.get("value") or "").strip().lower()
                        if v:
                            values.add(v)
                    sources_agree = len(values) <= 1
                    if sources_agree:
                        adjusted_conf = min(1.0, adjusted_conf + AGREEMENT_BONUS)
                        result.agreement_count += 1

            weighted_conf = adjusted_conf * weight
            weighted_sum += weighted_conf
            weight_total += weight

            result.field_scores[field_key] = FieldScore(
                field_key=field_key,
                raw_confidence=raw_conf,
                weighted_confidence=adjusted_conf,
                weight=weight,
                is_critical=is_critical,
                validation_passed=validation_passed,
                source_count=source_count,
                sources_agree=sources_agree,
            )

        result.total_fields = len(result.field_scores)

        # Check for missing critical fields
        # NOTE: We only apply the MISSING_CRITICAL_PENALTY below, NOT inflate
        # the denominator. Adding to weight_total AND applying penalty was a
        # double-penalty that crushed scores when any critical field was missing.
        for cf in critical_fields:
            if cf not in fields or not fields.get(cf, {}).get("value"):
                result.missing_critical_count += 1
                result.critical_fields_complete = False

        # Calculate overall confidence
        if weight_total > 0:
            result.overall_confidence = weighted_sum / weight_total
        else:
            result.overall_confidence = 0.0

        # Apply missing-field penalty
        if result.missing_critical_count > 0:
            penalty = result.missing_critical_count * MISSING_CRITICAL_PENALTY
            result.overall_confidence = max(0.0, result.overall_confidence - penalty)

        # Apply OCR quality penalty
        quality_penalty = self._compute_quality_penalty(ocr_quality)
        if quality_penalty > 0:
            result.overall_confidence = max(0.0, result.overall_confidence - quality_penalty)

        # Cap at 1.0
        result.overall_confidence = min(1.0, result.overall_confidence)

        # Generate review reasons
        result.review_reasons = self._get_review_reasons(
            result, payer_name, quality_penalty,
        )

        return result

    def determine_needs_review(
        self,
        scoring_result: ScoringResult,
        auto_finalize_threshold: float = 0.90,
        payer_name: str | None = None,
        candidates_by_field: dict[str, list[dict[str, Any]]] | None = None,
    ) -> bool:
        """
        Determine if the fax job needs human review.

        Includes agree-to-finalize check: if both TEMPLATE_OCR and LayoutLM/VLM
        candidates exist for a critical field, they must agree (after
        canonicalization) to allow auto-finalization.

        Args:
            scoring_result: Result from score().
            auto_finalize_threshold: Overall confidence threshold.
            payer_name: Payer for payer-specific thresholds.
            candidates_by_field: All candidates per field {field_key: [candidate_dicts]}.

        Returns:
            True if review is needed.
        """
        # Low overall confidence
        if scoring_result.overall_confidence < auto_finalize_threshold:
            return True

        # Missing critical fields
        if not scoring_result.critical_fields_complete:
            return True

        # Validation failures on critical fields — only trigger review if
        # the field also has LOW confidence.  High-confidence extractions
        # from template-matching are reliable; a validation-format mismatch
        # more likely means the regex rule is wrong, not the value.
        for fs in scoring_result.field_scores.values():
            if fs.is_critical and fs.validation_passed is False:
                if fs.weighted_confidence < 0.85:
                    return True

        # Per-field confidence floor check — only for CRITICAL fields.
        # Non-critical fields (e.g., LayoutLM extras, classifier outputs)
        # should not force review by themselves.
        for fs in scoring_result.field_scores.values():
            if not fs.is_critical:
                continue
            min_conf = 0.50
            if payer_name:
                payer_thresh = self.payer_rules.get_confidence_threshold(
                    payer_name, fs.field_key
                )
                # Use the stricter threshold: payer-specific or default floor,
                # but cap at 0.65 to avoid over-flagging from overly strict payer configs
                min_conf = min(max(payer_thresh, min_conf), 0.65)
            if fs.weighted_confidence < min_conf:
                return True

        # Agree-to-finalize: Template vs VLM must agree on critical fields
        if candidates_by_field:
            disagreements = self.check_critical_agreement(
                candidates_by_field, payer_name
            )
            if disagreements:
                for reason in disagreements:
                    scoring_result.review_reasons.append(reason)
                return True

        return False

    def check_critical_agreement(
        self,
        candidates_by_field: dict[str, list[dict[str, Any]]],
        payer_name: str | None = None,
    ) -> list[str]:
        """
        Check if TEMPLATE_OCR and LayoutLM/VLM candidates agree on critical fields.

        For auto-finalization safety, when both a template-based extraction and
        a LayoutLM-based extraction exists for the same critical field, their values
        must match (after canonicalization). Disagreement triggers review.

        Args:
            candidates_by_field: All candidates {field_key: [candidate_dicts]}.
            payer_name: Payer for determining critical fields.

        Returns:
            List of disagreement reason codes (empty = all agree).
        """
        critical_fields = self._get_critical_fields(payer_name)
        canonicalizer = FieldCanonicalizer()
        disagreements: list[str] = []

        template_methods = {"TEMPLATE_OCR"}
        vlm_methods = {"LAYOUTLM", "VLM"}

        for field_key in critical_fields:
            candidates = candidates_by_field.get(field_key, [])
            if not candidates:
                continue

            # Collect template and VLM values
            template_values = []
            vlm_values = []
            for c in candidates:
                method = c.get("method", "")
                value = (c.get("value") or "").strip()
                if not value:
                    continue
                if method in template_methods:
                    template_values.append(value)
                elif method in vlm_methods:
                    vlm_values.append(value)

            # Only check agreement when both sources exist
            if not template_values or not vlm_values:
                continue

            # Compare first template value against first VLM value
            # (after canonicalization)
            t_val = template_values[0]
            v_val = vlm_values[0]

            # Determine field type for canonicalization
            field_type = "text"
            if field_key.endswith("_date") or field_key.endswith("_dob"):
                field_type = "date"
            elif field_key == "member_id":
                field_type = "member_id"

            if not canonicalizer.are_equivalent(t_val, v_val, field_type):
                reason = f"TEMPLATE_VLM_DISAGREE_{field_key}"
                disagreements.append(reason)
                logger.warning(
                    "Agree-to-finalize FAILED for %s: template and VLM values disagree",
                    field_key,
                )

        return disagreements

    def _get_critical_fields(self, payer_name: str | None) -> list[str]:
        """Get critical fields, preferring payer-specific list."""
        if payer_name:
            payer = self.payer_rules.get_payer(payer_name)
            if payer and payer.critical_fields:
                return payer.critical_fields
        return DEFAULT_CRITICAL_FIELDS

    @staticmethod
    def _compute_quality_penalty(
        ocr_quality: dict[str, float] | None,
    ) -> float:
        """
        Compute confidence penalty based on OCR quality metrics.

        Penalizes blurry pages and low text density. The penalty scales
        linearly from 0 (good quality) to OCR_QUALITY_MAX_PENALTY (very poor).

        Args:
            ocr_quality: Dict with avg_blur_score, avg_text_density, avg_token_confidence.

        Returns:
            Penalty value (0.0 = no penalty, up to OCR_QUALITY_MAX_PENALTY).
        """
        if not ocr_quality:
            return 0.0

        penalty = 0.0

        # Blur penalty: low blur_score = blurry image = unreliable OCR
        avg_blur = ocr_quality.get("avg_blur_score", OCR_QUALITY_BLUR_THRESHOLD)
        if avg_blur < OCR_QUALITY_BLUR_THRESHOLD:
            # Scale: 0 blur → full penalty, threshold → no penalty
            blur_ratio = max(0.0, 1.0 - avg_blur / OCR_QUALITY_BLUR_THRESHOLD)
            penalty += blur_ratio * OCR_QUALITY_MAX_PENALTY * 0.5

        # Text density penalty: very sparse = likely bad OCR
        avg_density = ocr_quality.get("avg_text_density", 0.05)
        if avg_density < OCR_QUALITY_MIN_TEXT_DENSITY:
            penalty += OCR_QUALITY_MAX_PENALTY * 0.3

        # Token confidence penalty: low avg confidence = OCR uncertain
        avg_token_conf = ocr_quality.get("avg_token_confidence", 0.90)
        if avg_token_conf < 0.80:
            conf_gap = 0.80 - avg_token_conf  # max ~0.80
            penalty += min(conf_gap * 0.5, OCR_QUALITY_MAX_PENALTY * 0.5)

        return min(penalty, OCR_QUALITY_MAX_PENALTY)

    def _get_review_reasons(
        self,
        result: ScoringResult,
        payer_name: str | None,
        quality_penalty: float = 0.0,
    ) -> list[str]:
        """Generate review reason codes from scoring result."""
        reasons: list[str] = []

        if result.overall_confidence < 0.70:
            reasons.append("LOW_CONFIDENCE")

        if not result.critical_fields_complete:
            reasons.append("MISSING_CRITICAL_FIELDS")

        if result.validation_failure_count > 0:
            reasons.append("VALIDATION_FAILED")

        if quality_penalty > 0.05:
            reasons.append("LOW_OCR_QUALITY")

        # Check for source conflicts
        for fs in result.field_scores.values():
            if fs.source_count > 1 and not fs.sources_agree and fs.is_critical:
                reasons.append("SOURCE_CONFLICT")
                break

        # Per-field low confidence
        for fs in result.field_scores.values():
            if fs.weighted_confidence < 0.70:
                reasons.append(f"LOW_CONF_{fs.field_key.upper()}")

        if len(reasons) > 8:
            reasons = reasons[:7] + ["ADDITIONAL_ISSUES_TRUNCATED"]
        return reasons
