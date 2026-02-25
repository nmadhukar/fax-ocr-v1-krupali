"""
Stage 2: Classification - Payer detection, template matching, doc classification.

Steps 5-7 of the fax processing pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

import cv2
import numpy as np
from sqlalchemy import text as sa_text

from libs.shared.db.models.enums import (
    DocTypeEnum,
    ExtractionMethodEnum,
    PayerNameEnum,
)

from . import PipelineContext

logger = logging.getLogger(__name__)


def detect_payer(ctx: PipelineContext) -> None:
    """Step 5: Payer auto-detection."""
    ctx.detected_payer = ctx.job.payer_hint or PayerNameEnum.UNKNOWN

    if not ctx.all_page_text:
        return

    from libs.shared.classification.payer_detector import PayerDetector

    payer_detector = PayerDetector()
    payer_result = payer_detector.detect(ctx.all_page_text)
    ctx.payer_detection_meta = payer_result.to_dict()

    if ctx.detected_payer == PayerNameEnum.UNKNOWN and payer_result.payer != PayerNameEnum.UNKNOWN:
        ctx.detected_payer = payer_result.payer
        ctx.job.payer_hint = ctx.detected_payer

    ctx.update_payer_str()

    logger.info(
        "Payer detection: %s (%.2f via %s)",
        payer_result.payer.value,
        payer_result.confidence,
        payer_result.method,
    )


def match_template(ctx: PipelineContext) -> None:
    """Step 6: Template matching (multi-page)."""
    adaptive_cfg = getattr(ctx.settings, "adaptive", None)
    drift_enabled = bool(getattr(adaptive_cfg, "template_drift_enabled", False))
    ctx.content_page_images = {}

    for page_num in range(1, len(ctx.pages) + 1):
        if ctx.active_page_numbers and page_num not in ctx.active_page_numbers:
            continue
        page_record = ctx.page_repo.get_page_with_tokens(ctx.job_uuid, page_num)
        if page_record and not page_record.is_cover_page:
            ctx.content_page_images[page_num] = ctx.pages[page_num - 1]

    if not ctx.content_page_images:
        return

    match_result = ctx.template_matcher.match_best_page(
        ctx.content_page_images, ctx.db, ctx.payer_str,
    )
    if match_result is None:
        logger.warning(
            "Template matcher returned no result object for job %s",
            str(ctx.job_uuid)[:8],
        )
        return

    ctx.match_result = match_result

    if drift_enabled:
        ctx.template_drift_meta = {
            "match_result": ctx.match_result.to_dict(),
            "signals": [],
            "flagged": False,
        }

    if ctx.match_result.matched:
        ctx.job.matched_template_version_id = ctx.match_result.template_version_id
        ctx.job.matched_template_score = ctx.match_result.score

        if ctx.match_result.payer_name:
            try:
                ctx.job.payer_hint = PayerNameEnum(ctx.match_result.payer_name)
                ctx.detected_payer = ctx.job.payer_hint
            except ValueError:
                pass

        logger.info(
            "Template matched on page %d: %s (score=%.3f)",
            ctx.match_result.matched_page_number,
            ctx.match_result.template_name,
            ctx.match_result.score,
        )
    elif drift_enabled and ctx.template_drift_meta is not None:
        ctx.template_drift_meta["signals"].append("template_not_matched")
        ctx.template_drift_meta["flagged"] = True


def apply_page_rotation(ctx: PipelineContext) -> None:
    """Step 6b: Apply page rotation from template config."""
    if not (ctx.match_result and ctx.match_result.matched and ctx.match_result.template_version_id):
        return

    from libs.shared.db.models.fax_template import FaxTemplateVersion as _FTV
    from workers.fax_processing_worker.tasks.process_fax import _rotate_image

    tv = ctx.db.get(_FTV, ctx.match_result.template_version_id)
    rotate_config = (tv.template_config or {}).get("rotate_pages", {}) if tv else {}

    if not rotate_config:
        return

    logger.info("Applying page rotations from template config: %s", rotate_config)
    rotated_any = False
    for page_num_str, angle in rotate_config.items():
        page_num = int(page_num_str)
        if page_num > len(ctx.pages):
            continue

        original_image = ctx.pages[page_num - 1]
        rotated = _rotate_image(original_image, int(angle))
        if rotated is original_image:
            continue

        page_id = ctx.page_id_map.get(page_num)
        if page_id:
            try:
                processed_rot, _ = ctx.preprocessor.preprocess(rotated)
                if processed_rot is None:
                    processed_rot = rotated

                ocr_result_rot = ctx.ocr_client.extract(processed_rot)
                if not ocr_result_rot or not hasattr(ocr_result_rot, "tokens"):
                    raise RuntimeError("OCR returned no token payload")

                tokens_data_rot = []
                for token in ocr_result_rot.tokens:
                    token_dict = token.to_dict()
                    token_dict["fax_page_id"] = page_id
                    tokens_data_rot.append(token_dict)

                if not tokens_data_rot:
                    raise RuntimeError("OCR returned zero tokens after rotation")

                h, w = rotated.shape[:2]
                ctx.db.execute(sa_text(
                    "DELETE FROM fax_ocr_token WHERE fax_page_id = :pid"
                ), {"pid": str(page_id)})
                ctx.db.execute(sa_text(
                    "UPDATE fax_page SET width_px = :w, height_px = :h WHERE fax_page_id = :pid"
                ), {"w": w, "h": h, "pid": str(page_id)})
                ctx.token_repo.bulk_insert(tokens_data_rot)

                page_storage_key = f"{ctx.job.file_storage_key}_page_{page_num}.png"
                ok, rot_png = cv2.imencode(".png", rotated)
                if not ok or rot_png is None:
                    raise RuntimeError("Failed to encode rotated page image")
                ctx.storage.upload(
                    key=page_storage_key,
                    data=rot_png.tobytes(),
                    content_type="image/png",
                )

                ctx.db.flush()
                logger.info(
                    "Rotated page %d by %d degrees and re-OCR'd (%dx%d)",
                    page_num,
                    int(angle),
                    w,
                    h,
                )
            except Exception:
                logger.warning(
                    "Failed to rotate/re-OCR page %d for job %s; keeping original page data",
                    page_num,
                    str(ctx.job_uuid)[:8],
                    exc_info=True,
                )
                continue

        ctx.pages[page_num - 1] = rotated
        if page_num in ctx.content_page_images:
            ctx.content_page_images[page_num] = rotated
        rotated_any = True

    if rotated_any:
        from .ingestion import _rebuild_ocr_text

        _rebuild_ocr_text(ctx)


def classify_document(ctx: PipelineContext) -> None:
    """Step 7: Document classification."""
    if not ctx.all_ocr_text:
        ctx.job.doc_type = DocTypeEnum.UNKNOWN
        ctx.job.doc_type_conf = 0.0
        return

    from libs.shared.classification.doc_classifier import DocClassifier

    doc_classifier = DocClassifier()
    doc_result = doc_classifier.classify(ctx.all_ocr_text)
    effective_doc_type = doc_result.doc_type
    effective_conf = float(doc_result.confidence)

    # Self-learning assist: if image/template match is strong, use template doc_type
    # as a prior when text classifier is uncertain.
    if (
        ctx.match_result
        and ctx.match_result.matched
        and ctx.match_result.template_version_id
        and (effective_doc_type in (DocTypeEnum.UNKNOWN, DocTypeEnum.OTHER) or effective_conf < 0.55)
    ):
        try:
            from libs.shared.db.models.fax_template import FaxTemplateVersion

            tv = ctx.db.get(FaxTemplateVersion, ctx.match_result.template_version_id)
            template_doc_type = tv.template.doc_type if tv and tv.template else None
            if template_doc_type and template_doc_type != DocTypeEnum.UNKNOWN:
                effective_doc_type = template_doc_type
                effective_conf = max(effective_conf, min(0.95, float(ctx.match_result.score)))
                logger.info(
                    "Doc classification boosted from template prior: %s (%.2f)",
                    effective_doc_type.value,
                    effective_conf,
                )
        except Exception:
            logger.warning(
                "Template-prior doc classification fallback failed for job %s",
                str(ctx.job_uuid)[:8],
                exc_info=True,
            )

    ctx.job.doc_type = effective_doc_type
    ctx.job.doc_type_conf = effective_conf
    ctx.decision_value = doc_result.decision_value
    ctx.doc_class_meta = doc_result.to_dict()
    ctx.doc_class_meta["effective_doc_type"] = effective_doc_type.value
    ctx.doc_class_meta["effective_confidence"] = effective_conf

    logger.info(
        "Doc classification: %s (%.2f via %s)",
        effective_doc_type.value,
        effective_conf,
        doc_result.method,
    )


def route_page_sections(ctx: PipelineContext) -> None:
    """
    Route pages into sections and derive per-field page allowlists.

    This is additive safety logic to reduce field contamination from
    appeal/instruction pages while preserving fallback to all pages.
    """
    adaptive_cfg = getattr(ctx.settings, "adaptive", None)
    if not bool(getattr(adaptive_cfg, "section_routing_enabled", False)):
        return

    from libs.shared.classification.page_section_router import (
        KEY_FIELDS_SECTION_POLICY,
        PageSectionRouter,
    )

    page_texts: dict[int, str] = {}
    cover_pages: set[int] = set()
    for page_num in range(1, len(ctx.pages) + 1):
        if ctx.active_page_numbers and page_num not in ctx.active_page_numbers:
            continue
        page_record = ctx.page_repo.get_page_with_tokens(ctx.job_uuid, page_num)
        if not page_record:
            continue
        text = ""
        if hasattr(page_record, "get_full_text"):
            try:
                text = page_record.get_full_text() or ""
            except Exception:
                text = ""
        page_texts[page_num] = text
        if bool(getattr(page_record, "is_cover_page", False)):
            cover_pages.add(page_num)

    if not page_texts:
        return

    router = PageSectionRouter()
    routed = router.route_pages(page_texts, cover_pages=cover_pages)
    allowlist = router.build_field_allowlist(
        routed_pages=routed,
        candidate_fields=set(KEY_FIELDS_SECTION_POLICY.keys()),
    )

    ctx.page_sections = {pnum: res.section for pnum, res in routed.items()}
    ctx.page_section_scores = {pnum: res.scores for pnum, res in routed.items()}
    ctx.field_allowed_pages = allowlist

    logger.info(
        "Page routing: %s",
        ", ".join(
            f"p{p}={ctx.page_sections.get(p, 'other')}"
            for p in sorted(ctx.page_sections.keys())
        ),
    )
