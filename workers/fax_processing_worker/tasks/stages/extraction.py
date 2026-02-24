"""
Stage 3: Extraction — Template, OCR-label, LayoutLM, VLM screening.

Steps 8–9c of the fax processing pipeline.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from uuid import uuid4

from libs.shared.db.models.enums import ExtractionMethodEnum
from libs.shared.extraction.field_builder import ExtractionCandidate

from . import PipelineContext

logger = logging.getLogger(__name__)
_LAYOUTLM_STATE_LOCK = threading.Lock()
_LAYOUTLM_INFLIGHT_TOKEN: str | None = None
_LAYOUTLM_INFLIGHT_STARTED_MONO: float = 0.0


def _try_acquire_layoutlm_slot(timeout_seconds: int, fax_job_id: str) -> str | None:
    """Acquire the single LayoutLM in-flight slot with stale-run reclamation."""
    global _LAYOUTLM_INFLIGHT_TOKEN, _LAYOUTLM_INFLIGHT_STARTED_MONO

    now_mono = time.monotonic()
    stale_after_seconds = max(timeout_seconds + 30, int(timeout_seconds * 1.5))

    with _LAYOUTLM_STATE_LOCK:
        if _LAYOUTLM_INFLIGHT_TOKEN is not None:
            age_seconds = now_mono - _LAYOUTLM_INFLIGHT_STARTED_MONO
            if age_seconds < stale_after_seconds:
                logger.warning(
                    "LayoutLM skipped for job %s: previous extraction still in progress (age=%.1fs)",
                    fax_job_id,
                    age_seconds,
                )
                return None
            logger.warning(
                "LayoutLM reclaiming stale in-flight slot for job %s (age=%.1fs)",
                fax_job_id,
                age_seconds,
            )

        token = uuid4().hex
        _LAYOUTLM_INFLIGHT_TOKEN = token
        _LAYOUTLM_INFLIGHT_STARTED_MONO = now_mono
        return token


def _release_layoutlm_slot(token: str) -> None:
    """Release the in-flight slot only if this caller still owns it."""
    global _LAYOUTLM_INFLIGHT_TOKEN, _LAYOUTLM_INFLIGHT_STARTED_MONO

    with _LAYOUTLM_STATE_LOCK:
        if _LAYOUTLM_INFLIGHT_TOKEN != token:
            return
        _LAYOUTLM_INFLIGHT_TOKEN = None
        _LAYOUTLM_INFLIGHT_STARTED_MONO = 0.0


def template_extraction(ctx: PipelineContext) -> None:
    """Step 8: Template-based extraction with verification."""
    if not (ctx.match_result and ctx.match_result.matched and ctx.match_result.template_version_id):
        return

    verification = ctx.field_extractor.verify_template_match(
        ctx.match_result.template_version_id,
        ctx.payer_str,
        ctx.page_id_map,
        ctx.db,
        template_match_score=ctx.match_result.score,
    )
    ctx.template_verified = verification["verified"]

    logger.info(
        "Template verification: verified=%s, payer_match=%s, "
        "anchor_score=%.2f (%d/%d), reason=%s",
        verification["verified"],
        verification["payer_match"],
        verification["anchor_score"],
        verification["anchors_found"],
        verification["anchors_total"],
        verification["reason"],
    )

    if not ctx.template_verified:
        logger.warning(
            "Template match NOT verified (%s) — skipping template extraction",
            verification["reason"],
        )
        return

    ctx.extraction_results = ctx.field_extractor.extract_all_fields(
        ctx.match_result.template_version_id,
        ctx.page_id_map,
        ctx.db,
    )

    for result in ctx.extraction_results:
        if not result.value:
            continue
        candidate = ExtractionCandidate(
            value=result.value,
            method=ExtractionMethodEnum.TEMPLATE_OCR,
            confidence=result.confidence,
            evidence_bbox=result.evidence_bbox,
            evidence_text=result.evidence_text,
        )
        ctx.candidates_by_field.setdefault(result.field_key, []).append(candidate)
        ctx.raw_candidates_by_field.setdefault(result.field_key, []).append(
            candidate.to_dict()
        )

        ctx.field_repo.upsert_field(
            fax_job_id=ctx.job_uuid,
            field_key=result.field_key,
            method=ExtractionMethodEnum.TEMPLATE_OCR,
            field_value=result.value,
            field_conf=result.confidence,
            evidence_bbox=result.evidence_bbox,
            evidence_text=result.evidence_text,
        )

    logger.info(
        "Template extraction (%s): %d fields extracted",
        "label_anchored" if any(
            r.extraction_strategy == "label_anchored"
            for r in ctx.extraction_results if r.value
        ) else "roi",
        sum(1 for r in ctx.extraction_results if r.value),
    )


def ocr_label_extraction(ctx: PipelineContext) -> None:
    """Step 8b: OCR Label Extraction (always runs as secondary source)."""
    from libs.shared.extraction.ocr_label_extractor import (
        extract_fields_from_ocr_tokens,
    )

    label_tokens_by_page: dict[int, list[Any]] = {}
    for page_num in range(1, len(ctx.pages) + 1):
        if ctx.active_page_numbers and page_num not in ctx.active_page_numbers:
            continue
        page_record = ctx.page_repo.get_page_with_tokens(ctx.job_uuid, page_num)
        if page_record and not page_record.is_cover_page:
            if hasattr(page_record, "ocr_tokens") and page_record.ocr_tokens:
                label_tokens_by_page[page_num] = page_record.ocr_tokens

    if not label_tokens_by_page:
        return

    label_results = extract_fields_from_ocr_tokens(
        tokens_by_page=label_tokens_by_page,
        payer_name=ctx.payer_str,
    )

    for field_key, label_candidate in label_results.items():
        ctx.candidates_by_field.setdefault(field_key, []).append(label_candidate)
        ctx.raw_candidates_by_field.setdefault(field_key, []).append(
            label_candidate.to_dict()
        )

        # Only persist as primary if template didn't already extract this field
        if field_key not in {
            r.field_key for r in (ctx.extraction_results if ctx.template_verified else [])
            if r.value
        }:
            ctx.field_repo.upsert_field(
                fax_job_id=ctx.job_uuid,
                field_key=field_key,
                method=ExtractionMethodEnum.OCR_LABEL,
                field_value=label_candidate.value,
                field_conf=label_candidate.confidence,
                evidence_bbox=label_candidate.evidence_bbox,
                evidence_text=label_candidate.evidence_text,
            )

    logger.info(
        "OCR label extraction: %d fields (%s)",
        len(label_results),
        "secondary" if ctx.template_verified else "primary",
    )


def layoutlm_extraction(ctx: PipelineContext) -> None:
    """Step 9: LayoutLM Document QA extraction (gap filler)."""
    _LAYOUTLM_TIMEOUT_SECONDS = 90
    _LM_CONF_THRESHOLD = 0.75

    # Collect content page images + raw OCR tokens for LayoutLM
    content_page_images_list: list = []
    ctx.raw_ocr_tokens = []

    if ctx.settings.vlm.layoutlm_enabled:
        for page_num in range(1, len(ctx.pages) + 1):
            if ctx.active_page_numbers and page_num not in ctx.active_page_numbers:
                continue
            page_record = ctx.page_repo.get_page_with_tokens(ctx.job_uuid, page_num)
            if page_record and not page_record.is_cover_page:
                content_page_images_list.append(ctx.pages[page_num - 1])
                page_toks: list[dict] = []
                if hasattr(page_record, "ocr_tokens"):
                    for tok in page_record.ocr_tokens:
                        page_toks.append({
                            "text": tok.token_text,
                            "bbox_x0": float(tok.bbox_x0),
                            "bbox_y0": float(tok.bbox_y0),
                            "bbox_x1": float(tok.bbox_x1),
                            "bbox_y1": float(tok.bbox_y1),
                            "confidence": float(tok.confidence),
                        })
                ctx.raw_ocr_tokens.append(page_toks)

    # Determine which fields to query
    try:
        from libs.shared.extraction.layoutlm_extractor import (
            FIELD_QUESTIONS_KEYS as _LM_ALL_FIELDS,
        )
        fields_for_layoutlm: list[str] = [
            fk for fk in _LM_ALL_FIELDS
            if max(
                (c.confidence for c in ctx.candidates_by_field.get(fk, [])),
                default=0.0,
            ) < _LM_CONF_THRESHOLD
        ][:8]
    except Exception:
        fields_for_layoutlm = []

    if not ctx.settings.vlm.layoutlm_enabled:
        return

    if not content_page_images_list:
        logger.debug("LayoutLM skipped: no content page images for job %s", ctx.fax_job_id)
        return

    if not fields_for_layoutlm:
        logger.info(
            "LayoutLM skipped: all fields already at conf >= %.2f for job %s",
            _LM_CONF_THRESHOLD, ctx.fax_job_id,
        )
        return

    # Prevent unbounded buildup of timed-out daemon threads. If a prior
    # LayoutLM call is still running, skip this job's LayoutLM step unless the
    # slot has gone stale, in which case reclaim ownership.
    slot_token = _try_acquire_layoutlm_slot(_LAYOUTLM_TIMEOUT_SECONDS, str(ctx.fax_job_id))
    if slot_token is None:
        return

    # Import singleton loader from original module
    from workers.fax_processing_worker.tasks.process_fax import _get_layoutlm_extractor

    lm_result_holder: list[Any] = [None]
    lm_error_holder: list[Any] = [None]

    def _run_layoutlm() -> None:
        try:
            from libs.shared.extraction.layoutlm_extractor import OcrTokenForAlignment

            lm_extractor = _get_layoutlm_extractor(ctx.settings)
            lm_ocr_tokens = [
                [
                    OcrTokenForAlignment(
                        text=t["text"],
                        x0=t["bbox_x0"], y0=t["bbox_y0"],
                        x1=t["bbox_x1"], y1=t["bbox_y1"],
                        confidence=t["confidence"],
                    )
                    for t in page_toks
                ]
                for page_toks in ctx.raw_ocr_tokens
            ]
            lm_result_holder[0] = lm_extractor.extract(
                page_images=content_page_images_list,
                ocr_tokens_by_page=lm_ocr_tokens,
                payer_name=ctx.payer_str,
                fields_to_query=fields_for_layoutlm,
            )
        except Exception as e:
            lm_error_holder[0] = e
        finally:
            _release_layoutlm_slot(slot_token)

    lm_thread = threading.Thread(target=_run_layoutlm, daemon=True, name="layoutlm-extract")
    try:
        lm_thread.start()
    except Exception:
        _release_layoutlm_slot(slot_token)
        raise
    lm_thread.join(timeout=_LAYOUTLM_TIMEOUT_SECONDS)

    if lm_thread.is_alive():
        logger.warning(
            "LayoutLM extraction timed out after %ds for job %s; skipping",
            _LAYOUTLM_TIMEOUT_SECONDS, ctx.fax_job_id,
        )
    elif lm_error_holder[0] is not None:
        logger.warning(
            "LayoutLM extraction failed for job %s: %s; skipping",
            ctx.fax_job_id, lm_error_holder[0],
        )
    else:
        lm_result = lm_result_holder[0]
        if lm_result and lm_result.field_count > 0:
            ctx.layoutlm_extraction_meta = lm_result.to_dict()
            ctx.vlm_extraction_meta = ctx.layoutlm_extraction_meta

            for field_key, lm_candidate in lm_result.fields.items():
                ctx.candidates_by_field.setdefault(field_key, []).append(lm_candidate)
                ctx.raw_candidates_by_field.setdefault(field_key, []).append(
                    lm_candidate.to_dict()
                )

                ctx.field_repo.upsert_field(
                    fax_job_id=ctx.job_uuid,
                    field_key=field_key,
                    method=ExtractionMethodEnum.LAYOUTLM,
                    field_value=lm_candidate.value,
                    field_conf=lm_candidate.confidence,
                    evidence_bbox=lm_candidate.evidence_bbox,
                    evidence_text=lm_candidate.evidence_text,
                )

            logger.info(
                "LayoutLM extraction: %d fields from %d pages in %.0f ms",
                lm_result.field_count,
                lm_result.pages_processed,
                lm_result.latency_ms,
            )


def add_decision_from_classifier(ctx: PipelineContext) -> None:
    """Add decision from doc classifier if not already present."""
    if not ctx.decision_value or "decision" in ctx.candidates_by_field:
        return

    decision_conf = max(0.80, float(ctx.job.doc_type_conf or 0.80))
    candidate = ExtractionCandidate(
        value=ctx.decision_value,
        method=ExtractionMethodEnum.TEMPLATE_OCR,
        confidence=decision_conf,
    )
    ctx.candidates_by_field["decision"] = [candidate]
    ctx.raw_candidates_by_field["decision"] = [candidate.to_dict()]


def vlm_prescreening(ctx: PipelineContext) -> None:
    """Step 9c: Intelligent VLM pre-screening."""
    from libs.shared.extraction.field_builder import preprocess_vlm_candidates

    ctx.candidates_by_field = preprocess_vlm_candidates(ctx.candidates_by_field)
