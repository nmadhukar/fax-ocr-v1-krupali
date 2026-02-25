"""
Stage 6: Finalization — Store extraction, HITL flags, review/finalize, metrics.

Steps 16–17 of the fax processing pipeline.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from libs.shared.clients.prior_auth_client import PriorAuthClient
from libs.shared.clients.task_client import TaskClient
from libs.shared.db.models.enums import ExtractionMethodEnum, FaxJobStatusEnum
from libs.shared.db.models.fax_review import FaxReview
from libs.shared.db.repositories.extraction_repo import ExtractionRepository
from libs.shared.extraction.hitl import compute_field_flags
from libs.shared.scoring.confidence_scorer import ScoringResult

from . import PipelineContext

logger = logging.getLogger(__name__)


def store_extraction(ctx: PipelineContext) -> None:
    """Step 16a: Store the final extraction JSON."""
    model_versions = {
        "ocr": "PaddleOCR-PP-OCRv5",
        "layoutlm": (
            ctx.layoutlm_extraction_meta.get("model_name")
            if ctx.layoutlm_extraction_meta
            else None
        ),
        "classifier": "keyword-regex-v1",
        "embedder": "all-MiniLM-L6-v2",
        "pipeline": "3.0.0",
    }

    ctx.extraction_repo.upsert(
        fax_job_id=ctx.job_uuid,
        extraction_json=ctx.extracted_fields,
        model_versions=model_versions,
        pipeline_version="3.0.0",
    )

    # Keep fax_extracted_field synchronized with final post-processed values.
    # This avoids stale candidate rows (from pre-correction stages) leaking
    # into query/search endpoints that read from fax_extracted_field.
    ctx.field_repo.delete_by_job(ctx.job_uuid)
    for field_key, field_data in (ctx.extracted_fields or {}).items():
        if not isinstance(field_data, dict):
            continue

        method_raw = (field_data.get("method") or "").strip().upper()
        try:
            method_enum = ExtractionMethodEnum(method_raw)
        except Exception:
            method_enum = ExtractionMethodEnum.TEMPLATE_OCR

        field = ctx.field_repo.upsert_field(
            fax_job_id=ctx.job_uuid,
            field_key=field_key,
            method=method_enum,
            field_value=field_data.get("value"),
            field_conf=float(field_data.get("confidence")) if field_data.get("confidence") is not None else None,
            evidence_bbox=field_data.get("evidence_bbox"),
            evidence_text=field_data.get("evidence_text"),
        )
        field.validation_passed = field_data.get("validation_passed")
        field.validation_errors = field_data.get("validation_errors") or []


def hitl_flagging(ctx: PipelineContext) -> None:
    """Step 16b: HITL — compute per-field confidence flags."""
    if not ctx.settings.hitl.enabled:
        return

    if ctx.scoring_result is None:
        ctx.scoring_result = ScoringResult(
            overall_confidence=float(ctx.overall_conf or 0.0),
            review_reasons=[],
        )

    ctx.hitl_flags = compute_field_flags(
        ctx.extracted_fields,
        payer_name=ctx.detected_payer.value if ctx.detected_payer else None,
        default_threshold=ctx.settings.hitl.default_threshold,
        critical_threshold=ctx.settings.hitl.critical_threshold,
    )

    # Always sync stored flags so stale flags from prior runs are cleared.
    ctx.extraction_repo.update_flagged_fields(ctx.job_uuid, ctx.hitl_flags)

    if ctx.hitl_flags:
        logger.info(
            "HITL step 16b: %d field(s) flagged for review [job=%s]",
            len(ctx.hitl_flags),
            str(ctx.job_uuid)[:8],
        )

        # Force NEEDS_REVIEW if enough critical flags
        if (
            not ctx.needs_review
            and len(ctx.hitl_flags) >= ctx.settings.hitl.min_flags_for_review
        ):
            ctx.needs_review = True
            if ctx.scoring_result is not None:
                ctx.scoring_result.review_reasons.append("HITL_LOW_CONFIDENCE_FIELDS")
            logger.info(
                "HITL: overriding to NEEDS_REVIEW (%d flag(s)) [job=%s]",
                len(ctx.hitl_flags),
                str(ctx.job_uuid)[:8],
            )


def store_job_metadata(ctx: PipelineContext) -> None:
    """Store detection/classification metadata on the job."""
    ctx.job.job_metadata = {
        **(ctx.job.job_metadata or {}),
        "payer_detection": ctx.payer_detection_meta,
        "doc_classification": ctx.doc_class_meta,
        "document_splitting": ctx.split_meta,
        "vlm_extraction": (
            {
                "model": ctx.vlm_extraction_meta.get("model_name") if ctx.vlm_extraction_meta else None,
                "latency_ms": ctx.vlm_extraction_meta.get("latency_ms") if ctx.vlm_extraction_meta else None,
                "field_count": ctx.vlm_extraction_meta.get("field_count") if ctx.vlm_extraction_meta else None,
            }
            if ctx.vlm_extraction_meta
            else None
        ),
        "layoutlm_extraction": (
            {
                "model": ctx.layoutlm_extraction_meta.get("model_name") if ctx.layoutlm_extraction_meta else None,
                "latency_ms": ctx.layoutlm_extraction_meta.get("latency_ms") if ctx.layoutlm_extraction_meta else None,
                "field_count": ctx.layoutlm_extraction_meta.get("field_count") if ctx.layoutlm_extraction_meta else None,
            }
            if ctx.layoutlm_extraction_meta
            else None
        ),
        "page_sections": ctx.page_sections,
        "page_section_scores": ctx.page_section_scores,
        "field_allowed_pages": (
            {k: sorted(v) for k, v in (ctx.field_allowed_pages or {}).items()}
            if ctx.field_allowed_pages
            else {}
        ),
        "hard_field_adjudication": ctx.adjudication_meta,
        "template_drift": ctx.template_drift_meta,
        "cross_field_validation": ctx.cross_result.to_dict() if ctx.cross_result else None,
        "confidence_scoring": ctx.scoring_result.to_dict() if ctx.scoring_result else None,
    }


def create_review_or_finalize(ctx: PipelineContext) -> None:
    """Steps 16-17: Create review task or finalize job."""
    _review_reasons = (
        ctx.scoring_result.review_reasons[:8]
        if ctx.scoring_result is not None
        else []
    )

    if ctx.needs_review:
        ctx.job.status = FaxJobStatusEnum.NEEDS_REVIEW
        ctx.job.needs_review = True

        review_page_numbers = (
            sorted(p for p in ctx.active_page_numbers if p in ctx.page_id_map)
            if ctx.active_page_numbers
            else sorted(ctx.page_id_map.keys())
        )

        review_packet = {
            "fax_job_id": str(ctx.job_uuid),
            "pages": [
                {"page_number": p, "page_id": str(ctx.page_id_map[p])}
                for p in review_page_numbers
            ],
            "extracted_fields": ctx.extracted_fields,
            "template_match": ctx.match_result.to_dict() if ctx.match_result else None,
            "payer_detection": ctx.payer_detection_meta,
            "doc_classification": ctx.doc_class_meta,
            "flagged_fields": ctx.hitl_flags,
        }

        review = FaxReview(
            fax_job_id=ctx.job_uuid,
            review_packet=review_packet,
            review_reasons=_review_reasons,
        )
        ctx.review_repo.create(review)

        # Commit DB state BEFORE external API calls so that if the
        # external call fails the local DB is still consistent.
        ctx.job.processing_completed_at = datetime.now(timezone.utc)
        ctx.db.commit()

        try:
            task_client = TaskClient(db=ctx.db, tenant_id=ctx.tenant_id)
            task_client.create_review_task(
                fax_job_id=str(ctx.job_uuid),
                reason_codes=_review_reasons,
            )
        except Exception:
            logger.warning(
                "Failed to create external review task for job %s; "
                "local review record was persisted successfully",
                ctx.job_uuid, exc_info=True,
            )
    else:
        ctx.job.status = FaxJobStatusEnum.COMPLETED
        ctx.job.needs_review = False

        # Commit DB state BEFORE external API calls.
        ctx.job.processing_completed_at = datetime.now(timezone.utc)
        ctx.db.commit()

        try:
            prior_auth_client = PriorAuthClient(db=ctx.db, tenant_id=ctx.tenant_id)
            if not ctx.job.external_fax_id:
                case = prior_auth_client.create_case(
                    patient_name=(ctx.extracted_fields.get("patient_name") or {}).get("value"),
                    member_id=(ctx.extracted_fields.get("member_id") or {}).get("value"),
                    payer=ctx.detected_payer.value if ctx.detected_payer else None,
                    fax_job_id=str(ctx.job_uuid),
                )
                ctx.job.external_fax_id = case["case_id"]
                ctx.db.commit()  # persist external_fax_id
            prior_auth_client.attach_extraction(
                case_id=ctx.job.external_fax_id or str(ctx.job_uuid),
                extraction_json=ctx.extracted_fields,
                fax_job_id=str(ctx.job_uuid),
            )
        except Exception:
            logger.warning(
                "Failed to attach extraction to external system for job %s; "
                "local extraction was persisted successfully",
                ctx.job_uuid, exc_info=True,
            )


def finalize_metrics(ctx: PipelineContext) -> None:
    """Record pipeline metrics and log summary."""
    from libs.shared.db.models.enums import ExtractionMethodEnum

    ctx.metrics.total_duration_ms = (time.perf_counter() - ctx.pipeline_start) * 1000
    ctx.metrics.total_pages = len(ctx.pages)
    ctx.metrics.payer_detected = ctx.detected_payer.value if ctx.detected_payer else "UNKNOWN"
    ctx.metrics.payer_confidence = (
        ctx.payer_detection_meta.get("confidence", 0.0)
        if ctx.payer_detection_meta
        else 0.0
    )
    ctx.metrics.doc_type_detected = (
        ctx.job.doc_type.value if ctx.job.doc_type else "UNKNOWN"
    )
    ctx.metrics.doc_type_confidence = float(ctx.job.doc_type_conf or 0.0)
    ctx.metrics.template_matched = (
        ctx.match_result.matched if ctx.match_result else False
    )
    ctx.metrics.template_name = (
        ctx.match_result.template_name if ctx.match_result else None
    )
    ctx.metrics.template_match_score = (
        ctx.match_result.score if ctx.match_result else 0.0
    )
    ctx.metrics.template_match_page = (
        ctx.match_result.matched_page_number if ctx.match_result else None
    )
    ctx.metrics.template_verified = ctx.template_verified
    ctx.metrics.is_composite = ctx.split_meta.get("is_composite", False) if ctx.split_meta else False
    ctx.metrics.segment_count = ctx.split_meta.get("segment_count", 1) if ctx.split_meta else 1
    ctx.metrics.total_fields_extracted = len(ctx.extracted_fields)
    ctx.metrics.template_fields_extracted = sum(
        1 for r in (ctx.extraction_results if ctx.template_verified else [])
        if r.value
    )
    ctx.metrics.overall_confidence = ctx.overall_conf
    ctx.metrics.needs_review = ctx.needs_review
    ctx.metrics.review_reasons = ctx.scoring_result.review_reasons if ctx.scoring_result else []
    ctx.metrics.final_status = ctx.job.status.value
    if ctx.ocr_quality:
        ctx.metrics.avg_ocr_confidence = ctx.ocr_quality.get("avg_token_confidence", 0.0)
        ctx.metrics.avg_blur_score = ctx.ocr_quality.get("avg_blur_score", 0.0)

    # Store metrics in job metadata
    ctx.job.job_metadata = {
        **(ctx.job.job_metadata or {}),
        "pipeline_metrics": ctx.metrics.to_dict(),
    }
    ctx.db.commit()

    ctx.metrics.log_summary()
