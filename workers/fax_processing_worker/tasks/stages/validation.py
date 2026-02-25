"""
Stage 5: Validation — Field validation, cross-field checks, confidence scoring.

Steps 11–14 of the fax processing pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from libs.shared.extraction.canonicalizer import FieldCanonicalizer
from libs.shared.extraction.cross_field_validator import CrossFieldValidator
from libs.shared.extraction.validators import FieldValidator
from libs.shared.scoring.confidence_scorer import ConfidenceScorer
from libs.shared.scoring.confidence_scorer import ScoringResult

from . import PipelineContext

logger = logging.getLogger(__name__)


def validate_fields(ctx: PipelineContext) -> None:
    """Step 11: Field validation + canonicalization."""
    validator = FieldValidator()
    canonicalizer = FieldCanonicalizer()

    for field_key, field_data in ctx.extracted_fields.items():
        if not isinstance(field_data, dict):
            continue

        raw_value = field_data.get("value")
        if raw_value is None:
            continue
        value = raw_value if isinstance(raw_value, str) else str(raw_value)

        val_result = validator.validate(
            field_key=field_key,
            value=value,
            payer_name=ctx.payer_str,
            context={
                k: (v.get("value") if isinstance(v, dict) else None)
                for k, v in ctx.extracted_fields.items()
            },
        )

        # Persist validation state on the merged payload so confidence scoring
        # can apply validation-aware penalties.
        field_data["validation_passed"] = val_result.is_valid
        field_data["validation_errors"] = val_result.errors

        # Infer field_type from field_key for canonicalization
        _key_lower = field_key.lower()
        if "date" in _key_lower or "dob" in _key_lower:
            _field_type = "date"
        elif "phone" in _key_lower or _key_lower in ("fax_number", "fax_phone"):
            _field_type = "phone"
        elif "ssn" in _key_lower or "social" in _key_lower:
            _field_type = "ssn"
        elif "npi" in _key_lower:
            _field_type = "npi"
        elif "member_id" in _key_lower:
            _field_type = "member_id"
        else:
            _field_type = "text"

        canonical_value = canonicalizer.canonicalize(
            value=value,
            field_type=_field_type,
            payer_name=ctx.payer_str,
        )
        if canonical_value and canonical_value != value:
            field_data["canonical_value"] = canonical_value

        ctx.field_repo.update_validation(
            fax_job_id=ctx.job_uuid,
            field_key=field_key,
            validation_passed=val_result.is_valid,
            validation_errors=val_result.errors,
        )


def cross_field_checks(ctx: PipelineContext) -> None:
    """Step 13: Cross-field consistency checks."""
    cross_validator = CrossFieldValidator()
    ctx.cross_result = cross_validator.validate(ctx.extracted_fields)

    if not ctx.cross_result.is_consistent:
        logger.warning("Cross-field inconsistencies: %s", ctx.cross_result.errors)
        # Ensure cross-field problems influence review routing
        ctx.needs_review = True


def confidence_scoring(ctx: PipelineContext) -> None:
    """Step 14: Weighted confidence scoring (with OCR quality)."""
    # Compute OCR quality metrics from page records
    blur_scores = []
    text_densities = []
    token_confidences = []

    for page_num in range(1, len(ctx.pages) + 1):
        if ctx.active_page_numbers and page_num not in ctx.active_page_numbers:
            continue
        pr = ctx.page_repo.get_page_with_tokens(ctx.job_uuid, page_num)
        if pr and not pr.is_cover_page:
            if pr.blur_score is not None:
                blur_scores.append(float(pr.blur_score))
            if pr.text_density is not None:
                text_densities.append(float(pr.text_density))
            if hasattr(pr, "ocr_tokens") and pr.ocr_tokens:
                avg_conf = sum(float(t.confidence) for t in pr.ocr_tokens) / len(pr.ocr_tokens)
                token_confidences.append(avg_conf)

    if blur_scores or text_densities or token_confidences:
        ctx.ocr_quality = {}
        if blur_scores:
            ctx.ocr_quality["avg_blur_score"] = sum(blur_scores) / len(blur_scores)
        if text_densities:
            ctx.ocr_quality["avg_text_density"] = sum(text_densities) / len(text_densities)
        if token_confidences:
            ctx.ocr_quality["avg_token_confidence"] = sum(token_confidences) / len(token_confidences)

    scorer = ConfidenceScorer()
    try:
        ctx.scoring_result = scorer.score(
            fields=ctx.extracted_fields,
            payer_name=ctx.payer_str,
            candidates_by_field=ctx.raw_candidates_by_field,
            ocr_quality=ctx.ocr_quality,
        )
    except Exception:
        logger.warning(
            "Confidence scoring failed for job %s; forcing review fallback",
            str(ctx.job_uuid)[:8],
            exc_info=True,
        )
        ctx.scoring_result = ScoringResult(
            overall_confidence=0.0,
            review_reasons=["SCORING_FAILURE"],
        )
    ctx.overall_conf = ctx.scoring_result.overall_confidence
    ctx.job.overall_conf = ctx.overall_conf


def determine_review(ctx: PipelineContext) -> None:
    """Step 15: Determine if review is needed."""
    if ctx.scoring_result is None:
        ctx.scoring_result = ScoringResult(
            overall_confidence=0.0,
            review_reasons=["SCORING_RESULT_MISSING"],
        )

    _match_score = ctx.match_result.score if (ctx.match_result and ctx.match_result.matched) else 0.0
    _candidates_for_review = (
        ctx.raw_candidates_by_field if _match_score < 0.85 else None
    )

    scorer = ConfidenceScorer()
    ctx.needs_review = scorer.determine_needs_review(
        scoring_result=ctx.scoring_result,
        auto_finalize_threshold=ctx.settings.confidence.auto_finalize,
        payer_name=ctx.payer_str,
        candidates_by_field=_candidates_for_review,
    )

    if ctx.cross_result is not None and not ctx.cross_result.is_consistent:
        ctx.needs_review = True
        if "CROSS_FIELD_INCONSISTENCY" not in ctx.scoring_result.review_reasons:
            ctx.scoring_result.review_reasons.append("CROSS_FIELD_INCONSISTENCY")
