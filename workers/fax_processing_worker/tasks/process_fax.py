"""
Main fax processing task.

Orchestrates the complete processing pipeline (NO paid APIs):

  1.  Load fax from storage
  2.  Split into pages
  3.  Preprocess (deskew, denoise)
  4.  OCR extraction (PaddleOCR PP-OCRv5)
  4b. Generate embeddings (sentence-transformers all-MiniLM-L6-v2)
  5.  Payer auto-detection          (keyword-based)
  6.  Template matching             (pHash + ORB/FLANN)
  7.  Document classification       (keyword/regex stub)
  8.  Template-based field extraction (PRIMARY)
  10. Multi-source merge via FieldBuilder
  11. Field validation + canonicalization
  12. Cross-field consistency checks
  13. Weighted confidence scoring
  14. Determine review routing
  15. Store final extraction
  16. Create review / call TaskClient stub if needed
  17. Finalize / call PriorAuthClient stub if auto-approved
"""

import io
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

# Pre-import torch BEFORE any PaddleOCR/PaddlePaddle usage.
# On Windows, PaddlePaddle DLLs conflict with PyTorch DLLs if loaded first.
try:
    import torch  # noqa: F401
except ImportError:
    pass

import cv2
import numpy as np
from celery import shared_task
from PIL import Image

from libs.shared.config import get_settings
from libs.shared.db.models.enums import (
    DocTypeEnum,
    ExtractionMethodEnum,
    FaxJobStatusEnum,
    PayerNameEnum,
)
from libs.shared.db.models.fax_page import FaxPage
from libs.shared.db.models.fax_review import FaxReview
from libs.shared.db.repositories.extraction_repo import (
    ExtractedFieldRepository,
    ExtractionRepository,
)
from libs.shared.db.repositories.fax_job_repo import FaxJobRepository
from libs.shared.db.repositories.fax_page_repo import FaxPageRepository
from libs.shared.db.repositories.ocr_token_repo import OcrTokenRepository
from libs.shared.db.repositories.review_repo import ReviewRepository
from libs.shared.db.session import get_db_session
from libs.shared.extraction.field_builder import ExtractionCandidate, FieldBuilder
from libs.shared.extraction.template_extractor import TemplateExtractor
from libs.shared.ocr.paddle_client import PaddleOcrClient
from libs.shared.scoring.confidence_scorer import ConfidenceScorer
from libs.shared.storage.s3_adapter import S3StorageAdapter
from libs.shared.template.matcher import TemplateMatcher
from libs.shared.utils.image_utils import ImagePreprocessor

logger = logging.getLogger(__name__)

# Module-level LayoutLM singleton — loaded once per worker process, reused across tasks.
# Avoids ~30-120s model reload on every job.
_LAYOUTLM_CLIENT_SINGLETON: Any = None
_LAYOUTLM_EXTRACTOR_SINGLETON: Any = None


def _get_layoutlm_extractor(settings: Any) -> Any:
    """Return the cached LayoutLMExtractor, loading model on first call."""
    global _LAYOUTLM_CLIENT_SINGLETON, _LAYOUTLM_EXTRACTOR_SINGLETON
    if _LAYOUTLM_EXTRACTOR_SINGLETON is None:
        from libs.shared.extraction.layoutlm_extractor import LayoutLMExtractor
        from libs.shared.vlm.layoutlm_client import LayoutLMClient
        from libs.shared.vlm.base import VlmConfig
        lm_config = VlmConfig(model_name=settings.vlm.layoutlm_model_name)
        if settings.vlm.layoutlm_adapter_path:
            lm_config.adapter_path = settings.vlm.layoutlm_adapter_path
        _LAYOUTLM_CLIENT_SINGLETON = LayoutLMClient(config=lm_config)
        _LAYOUTLM_EXTRACTOR_SINGLETON = LayoutLMExtractor(
            layoutlm_client=_LAYOUTLM_CLIENT_SINGLETON,
            max_pages=min(settings.vlm.max_pages, 3),
        )
        logger.warning("LayoutLM model loaded into singleton — will reuse across tasks")
    return _LAYOUTLM_EXTRACTOR_SINGLETON


# -----------------------------------------------------------------------
# Celery task
# -----------------------------------------------------------------------

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(OSError, ConnectionError, TimeoutError, RuntimeError),
    dont_autoretry_for=(ValueError, TypeError, KeyError),
)
def process_fax_task(self, fax_job_id: str, tenant_id: str) -> dict[str, Any]:
    """
    Main fax processing task.

    Args:
        fax_job_id: UUID of the fax job to process.
        tenant_id: Tenant identifier.

    Returns:
        Dictionary with processing results.
    """
    job_uuid = UUID(fax_job_id)
    settings = get_settings()

    with get_db_session() as db:
        # Load fax job
        job_repo = FaxJobRepository(db)
        job = job_repo.get_by_id(job_uuid)

        if not job:
            raise ValueError(f"Fax job not found: {fax_job_id}")

        # Mark as processing
        job_repo.mark_processing_started(job_uuid)
        db.commit()

        try:
            import time as _time

            from libs.shared.monitoring.pipeline_metrics import (
                PipelineMetrics,
                StepTimer,
            )

            pipeline_start = _time.perf_counter()
            metrics = PipelineMetrics(
                fax_job_id=fax_job_id,
                tenant_id=tenant_id,
            )

            # Initialize Week-1 components
            storage = S3StorageAdapter()
            preprocessor = ImagePreprocessor()
            ocr_client = PaddleOcrClient()
            template_matcher = TemplateMatcher()
            field_extractor = TemplateExtractor()

            # Pipeline uses only open-source models:
            # PaddleOCR, LayoutLM, sentence-transformers.

            # -------------------------------------------------------
            # 1. Load file from storage
            # -------------------------------------------------------
            file_bytes = storage.download(job.file_storage_key)

            # -------------------------------------------------------
            # 2. Split into pages
            # -------------------------------------------------------
            pages = split_to_pages(file_bytes, job.original_filename)
            job.total_pages = len(pages)

            # -------------------------------------------------------
            # 3 & 4. Preprocess each page + OCR extraction
            # -------------------------------------------------------
            page_repo = FaxPageRepository(db)
            token_repo = OcrTokenRepository(db)
            page_id_map: dict[int, UUID] = {}

            # Clean up any partial data from previous attempts (retry-safe)
            from sqlalchemy import text as sa_text

            db.execute(sa_text(
                "DELETE FROM fax_review WHERE fax_job_id = :jid"
            ), {"jid": str(job_uuid)})
            db.execute(sa_text(
                "DELETE FROM fax_extracted_field WHERE fax_job_id = :jid"
            ), {"jid": str(job_uuid)})
            db.execute(sa_text(
                "DELETE FROM fax_ocr_token WHERE fax_page_id IN "
                "(SELECT fax_page_id FROM fax_page WHERE fax_job_id = :jid)"
            ), {"jid": str(job_uuid)})
            db.execute(sa_text(
                "DELETE FROM fax_page WHERE fax_job_id = :jid"
            ), {"jid": str(job_uuid)})
            db.flush()

            for page_num, page_image in enumerate(pages, start=1):
                height, width = page_image.shape[:2]

                # Store original page image
                page_storage_key = f"{job.file_storage_key}_page_{page_num}.png"
                _, page_png = cv2.imencode(".png", page_image)
                storage.upload(
                    key=page_storage_key,
                    data=page_png.tobytes(),
                    content_type="image/png",
                )

                # Preprocess
                processed, page_quality = preprocessor.preprocess(page_image)

                # Create page record
                fax_page = FaxPage(
                    fax_job_id=job_uuid,
                    page_number=page_num,
                    page_storage_key=page_storage_key,
                    width_px=width,
                    height_px=height,
                    dpi=300,
                    blur_score=page_quality.blur_score if page_quality else None,
                    skew_angle_deg=page_quality.skew_angle_deg if page_quality else None,
                    text_density=page_quality.text_density if page_quality else None,
                    is_cover_page=preprocessor.detect_cover_page(page_image),
                )
                page_repo.create(fax_page)
                page_id_map[page_num] = fax_page.fax_page_id

                # OCR extraction (with null safety)
                ocr_result = ocr_client.extract(processed)

                tokens_data = []
                if ocr_result is None or not hasattr(ocr_result, "tokens"):
                    logger.warning(
                        "OCR returned None for page %d of job %s",
                        page_num, fax_job_id,
                    )
                    continue

                for token in ocr_result.tokens:
                    token_dict = token.to_dict()
                    token_dict["fax_page_id"] = fax_page.fax_page_id
                    tokens_data.append(token_dict)

                if tokens_data:
                    token_repo.bulk_insert(tokens_data)

            db.commit()

            # -------------------------------------------------------
            # Gather OCR text from all pages.
            # - all_page_text: includes cover pages (for payer detection,
            #   since payer branding often appears on covers)
            # - all_ocr_text: excludes cover pages (for doc classification,
            #   extraction)
            # -------------------------------------------------------
            all_page_text = ""
            all_ocr_text = ""
            first_content_page_text = ""
            first_content_page = None

            for page_num in range(1, len(pages) + 1):
                page_record = page_repo.get_page_with_tokens(job_uuid, page_num)
                if page_record:
                    page_text = page_record.get_full_text()
                    all_page_text += page_text + "\n\n"
                    if not page_record.is_cover_page:
                        all_ocr_text += page_text + "\n\n"
                        if first_content_page is None:
                            first_content_page = page_record
                            first_content_page_text = page_text

            all_page_text = all_page_text.strip()
            all_ocr_text = all_ocr_text.strip()
            # Save full pre-composite OCR text for step 10d recovery scans.
            # Composite splitting may later restrict all_ocr_text to active
            # segment pages only, losing content from other pages.
            _full_doc_ocr_text = all_ocr_text

            # -------------------------------------------------------
            # 3b. OCR-based cover page re-detection
            # -------------------------------------------------------
            # detect_cover_page() only checks pixel text density (< 0.01),
            # which misses fax transmission headers that are full of text
            # (fax To/From/Subject, confidentiality notice, etc.).  Those
            # pages contain recipient names, dates, and phone numbers that
            # contaminate LayoutLM and template extraction.
            # After OCR we can reliably detect them by text pattern.
            import re as _re_cover

            _COVER_SIGNALS = [
                (r"fax\s+cover\s+sheet", 2.0),
                (r"\bcover\s+sheet\b", 1.5),
                (r"facsimile\s+transmission", 1.5),
                (r"confidentiality\s+notice", 1.0),
                (r"(?m)^to\s*:", 0.5),       # "To: PROVIDER"
                (r"(?m)^from\s*:", 0.5),     # "From: Payer"
                (r"(?m)^subject\s*:", 0.5),  # "Subject: ..."
                (r"(?m)^pages?\s*:", 0.5),   # "Pages: 4"
            ]
            _AUTH_CONTENT_KEYWORDS = [
                "authorization", "health plan id", "member name", "member id",
                "prior auth", "reference#", "cpt code", "hcpcs",
                "procedure code", "service code", "diagnosis",
                "auth status", "approved", "auth #",
            ]

            for _ocr_page_num in range(1, len(pages) + 1):
                _ocr_pr = page_repo.get_page_with_tokens(job_uuid, _ocr_page_num)
                if not _ocr_pr or _ocr_pr.is_cover_page:
                    continue  # already marked or missing
                _ocr_pt = _ocr_pr.get_full_text()
                _cover_score = sum(
                    _w
                    for _pat, _w in _COVER_SIGNALS
                    if _re_cover.search(_pat, _ocr_pt, _re_cover.IGNORECASE)
                )
                _has_auth = any(
                    _kw in _ocr_pt.lower() for _kw in _AUTH_CONTENT_KEYWORDS
                )
                if _cover_score >= 1.5 and not _has_auth:
                    try:
                        page_repo.mark_as_cover_page(_ocr_pr.fax_page_id)
                        logger.info(
                            "OCR cover re-detection: page %d marked as cover "
                            "(score=%.1f, no auth content)",
                            _ocr_page_num, _cover_score,
                        )
                    except Exception:
                        logger.warning(
                            "OCR cover re-detection: failed to mark page %d as cover "
                            "(continuing without mark)",
                            _ocr_page_num, exc_info=True,
                        )

            # Rebuild all_ocr_text / all_page_text after cover re-detection
            # so any newly-marked cover pages are excluded from extraction.
            all_page_text = ""
            all_ocr_text = ""
            first_content_page = None
            first_content_page_text = ""
            for _rb_page_num in range(1, len(pages) + 1):
                _rb_pr = page_repo.get_page_with_tokens(job_uuid, _rb_page_num)
                if _rb_pr:
                    _rb_pt = _rb_pr.get_full_text()
                    all_page_text += _rb_pt + "\n\n"
                    if not _rb_pr.is_cover_page:
                        all_ocr_text += _rb_pt + "\n\n"
                        if first_content_page is None:
                            first_content_page = _rb_pr
                            first_content_page_text = _rb_pt
            all_page_text = all_page_text.strip()
            all_ocr_text = all_ocr_text.strip()
            _full_doc_ocr_text = all_ocr_text
            db.commit()

            # -------------------------------------------------------
            # 4a. Composite document detection
            # -------------------------------------------------------
            # For multi-page faxes, detect if multiple documents are
            # stacked together. If composite, narrow to the primary
            # segment for payer detection + extraction.
            split_meta: dict[str, Any] | None = None
            active_page_numbers: set[int] | None = None  # None = all pages

            if len(pages) >= 4:
                from libs.shared.classification.document_splitter import DocumentSplitter

                splitter = DocumentSplitter()
                page_texts_for_split: dict[int, str] = {}
                cover_page_set: set[int] = set()

                for page_num in range(1, len(pages) + 1):
                    pr = page_repo.get_page_with_tokens(job_uuid, page_num)
                    if pr:
                        page_texts_for_split[page_num] = pr.get_full_text()
                        if pr.is_cover_page:
                            cover_page_set.add(page_num)

                split_result = splitter.detect_segments(
                    page_texts_for_split, cover_page_set
                )
                split_meta = split_result.to_dict()

                if split_result.is_composite:
                    # Pick the segment with highest payer confidence
                    best_seg = max(
                        split_result.segments,
                        key=lambda s: s.payer_confidence,
                    )
                    active_page_numbers = set(best_seg.content_pages)

                    logger.info(
                        "Composite fax: %d segments detected, focusing on "
                        "segment %d (pages %s, payer=%s)",
                        len(split_result.segments),
                        best_seg.segment_index,
                        best_seg.page_numbers,
                        best_seg.detected_payer,
                    )

                    # Rebuild OCR text from active pages only
                    all_page_text = ""
                    all_ocr_text = ""
                    first_content_page = None
                    for page_num in sorted(active_page_numbers):
                        pr = page_repo.get_page_with_tokens(job_uuid, page_num)
                        if pr:
                            pt = pr.get_full_text()
                            all_page_text += pt + "\n\n"
                            if not pr.is_cover_page:
                                all_ocr_text += pt + "\n\n"
                                if first_content_page is None:
                                    first_content_page = pr
                    all_page_text = all_page_text.strip()
                    all_ocr_text = all_ocr_text.strip()

            # -------------------------------------------------------
            # 4b. Generate embeddings (sentence-transformers)
            # -------------------------------------------------------
            embedding_count = 0
            if settings.features.vector_search and all_ocr_text:
                try:
                    from libs.shared.embeddings import get_embedding_client
                    from libs.shared.db.repositories.embedding_repo import EmbeddingRepository

                    emb_client = get_embedding_client()
                    if emb_client.is_available():
                        emb_repo = EmbeddingRepository(db)
                        clean_text = emb_client.clean_ocr_text(all_ocr_text)
                        chunks = emb_client.chunk_text(clean_text)
                        if chunks:
                            vectors = emb_client.encode(chunks)
                            emb_rows = []
                            for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
                                emb_rows.append({
                                    "fax_job_id": job_uuid,
                                    "fax_page_id": None,
                                    "embedding": vec,
                                    "source_text": chunk,
                                    "source_type": "page_text",
                                    "chunk_index": idx,
                                    "token_count": len(chunk.split()),
                                })
                            embedding_count = emb_repo.bulk_insert_embeddings(emb_rows)
                            db.flush()
                            logger.info(
                                "Generated %d embeddings (%d chunks)",
                                embedding_count,
                                len(chunks),
                            )
                    else:
                        logger.info("Embedding model not available; skipping")
                except Exception:
                    logger.warning(
                        "Embedding generation failed; skipping",
                        exc_info=True,
                    )

            # -------------------------------------------------------
            # 5. Payer auto-detection
            # -------------------------------------------------------
            detected_payer = job.payer_hint or PayerNameEnum.UNKNOWN
            payer_detection_meta: dict[str, Any] | None = None

            if all_page_text:
                from libs.shared.classification.payer_detector import PayerDetector

                payer_detector = PayerDetector()
                payer_result = payer_detector.detect(
                    all_page_text,
                )
                payer_detection_meta = payer_result.to_dict()

                # Only override if job didn't already have a known payer
                if detected_payer == PayerNameEnum.UNKNOWN and payer_result.payer != PayerNameEnum.UNKNOWN:
                    detected_payer = payer_result.payer
                    job.payer_hint = detected_payer
                elif detected_payer != PayerNameEnum.UNKNOWN:
                    # Preserve the user-provided hint
                    pass

                logger.info(
                    "Payer detection: %s (%.2f via %s)",
                    payer_result.payer.value,
                    payer_result.confidence,
                    payer_result.method,
                )

            # Payer string for use throughout extraction
            payer_str = (
                detected_payer.value
                if detected_payer != PayerNameEnum.UNKNOWN
                else None
            )

            # -------------------------------------------------------
            # 6. Template matching (multi-page: tries non-cover pages)
            #    If composite detected, only match against active segment
            # -------------------------------------------------------
            match_result = None
            content_page_images: dict[int, np.ndarray] = {}
            for page_num in range(1, len(pages) + 1):
                # Skip pages outside active segment (composite mode)
                if active_page_numbers and page_num not in active_page_numbers:
                    continue
                page_record = page_repo.get_page_with_tokens(job_uuid, page_num)
                if page_record and not page_record.is_cover_page:
                    content_page_images[page_num] = pages[page_num - 1]

            if content_page_images:
                match_result = template_matcher.match_best_page(
                    content_page_images, db, payer_str,
                )

                if match_result.matched:
                    job.matched_template_version_id = match_result.template_version_id
                    job.matched_template_score = match_result.score

                    if match_result.payer_name:
                        try:
                            job.payer_hint = PayerNameEnum(match_result.payer_name)
                            detected_payer = job.payer_hint
                        except ValueError:
                            pass

                    logger.info(
                        "Template matched on page %d: %s (score=%.3f)",
                        match_result.matched_page_number,
                        match_result.template_name,
                        match_result.score,
                    )

            # -------------------------------------------------------
            # 6b. Apply page rotation from template config
            # -------------------------------------------------------
            if match_result and match_result.matched and match_result.template_version_id:
                from libs.shared.db.models.fax_template import FaxTemplateVersion as _FTV

                tv = db.get(_FTV, match_result.template_version_id)
                rotate_config = (tv.template_config or {}).get("rotate_pages", {}) if tv else {}

                if rotate_config:
                    logger.info("Applying page rotations from template config: %s", rotate_config)
                    for page_num_str, angle in rotate_config.items():
                        page_num = int(page_num_str)
                        if page_num > len(pages):
                            continue

                        # Rotate the page image
                        rotated = _rotate_image(pages[page_num - 1], int(angle))
                        if rotated is pages[page_num - 1]:
                            continue  # No rotation needed
                        pages[page_num - 1] = rotated

                        # Update content_page_images if this page was used for matching
                        if page_num in content_page_images:
                            content_page_images[page_num] = rotated

                        # Re-OCR the rotated page: delete old tokens, update dimensions, re-extract
                        page_id = page_id_map.get(page_num)
                        if page_id:
                            h, w = rotated.shape[:2]
                            db.execute(sa_text(
                                "DELETE FROM fax_ocr_token WHERE fax_page_id = :pid"
                            ), {"pid": str(page_id)})
                            db.execute(sa_text(
                                "UPDATE fax_page SET width_px = :w, height_px = :h WHERE fax_page_id = :pid"
                            ), {"w": w, "h": h, "pid": str(page_id)})

                            # Re-OCR the rotated page
                            processed_rot, _ = preprocessor.preprocess(rotated)
                            ocr_result_rot = ocr_client.extract(processed_rot)
                            if ocr_result_rot and hasattr(ocr_result_rot, "tokens"):
                                tokens_data_rot = []
                                for token in ocr_result_rot.tokens:
                                    token_dict = token.to_dict()
                                    token_dict["fax_page_id"] = page_id
                                    tokens_data_rot.append(token_dict)
                                if tokens_data_rot:
                                    token_repo.bulk_insert(tokens_data_rot)

                            # Re-upload rotated page image
                            page_storage_key = f"{job.file_storage_key}_page_{page_num}.png"
                            _, rot_png = cv2.imencode(".png", rotated)
                            storage.upload(
                                key=page_storage_key,
                                data=rot_png.tobytes(),
                                content_type="image/png",
                            )

                            db.flush()
                            logger.info(
                                "Rotated page %d by %d° and re-OCR'd (%dx%d)",
                                page_num, int(angle), w, h,
                            )

            # -------------------------------------------------------
            # 7. Document classification
            # -------------------------------------------------------
            doc_class_meta: dict[str, Any] | None = None
            decision_value: str | None = None

            if all_ocr_text:
                from libs.shared.classification.doc_classifier import DocClassifier

                doc_classifier = DocClassifier()
                doc_result = doc_classifier.classify(
                    all_ocr_text,
                )
                job.doc_type = doc_result.doc_type
                job.doc_type_conf = doc_result.confidence
                decision_value = doc_result.decision_value
                doc_class_meta = doc_result.to_dict()

                logger.info(
                    "Doc classification: %s (%.2f via %s)",
                    doc_result.doc_type.value,
                    doc_result.confidence,
                    doc_result.method,
                )
            else:
                job.doc_type = DocTypeEnum.UNKNOWN
                job.doc_type_conf = 0.0

            # -------------------------------------------------------
            # 8-9. Field Extraction (Template primary + LayoutLM gap-fill)
            #
            # Collect ALL candidates per field, then merge via FieldBuilder
            # -------------------------------------------------------
            field_repo = ExtractedFieldRepository(db)
            # candidates_by_field: {field_key: [ExtractionCandidate, ...]}
            candidates_by_field: dict[str, list[ExtractionCandidate]] = {}
            # raw dicts for scorer agreement check
            raw_candidates_by_field: dict[str, list[dict[str, Any]]] = {}

            # --- 8. Template-based extraction (with verification) ---
            template_verified = False
            extraction_results: list = []
            if match_result and match_result.matched and match_result.template_version_id:
                # Verify the template actually applies to this document
                verification = field_extractor.verify_template_match(
                    match_result.template_version_id,
                    payer_str,
                    page_id_map,
                    db,
                    template_match_score=match_result.score,
                )
                template_verified = verification["verified"]

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

                if template_verified:
                    # Smart label-anchored extraction with ROI fallback
                    extraction_results = field_extractor.extract_all_fields(
                        match_result.template_version_id,
                        page_id_map,
                        db,
                    )

                    for result in extraction_results:
                        if result.value:
                            candidate = ExtractionCandidate(
                                value=result.value,
                                method=ExtractionMethodEnum.TEMPLATE_OCR,
                                confidence=result.confidence,
                                evidence_bbox=result.evidence_bbox,
                                evidence_text=result.evidence_text,
                            )
                            candidates_by_field.setdefault(result.field_key, []).append(candidate)
                            raw_candidates_by_field.setdefault(result.field_key, []).append(
                                candidate.to_dict()
                            )

                            field_repo.upsert_field(
                                fax_job_id=job_uuid,
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
                            for r in extraction_results if r.value
                        ) else "roi",
                        sum(1 for r in extraction_results if r.value),
                    )
                else:
                    logger.warning(
                        "Template match NOT verified (%s) — skipping template extraction",
                        verification["reason"],
                    )

            # --- 8b. OCR Label Extraction (always runs as secondary source) ---
            # Runs alongside template extraction to provide additional candidates
            # for cross-validation, and as primary source when no template matches.
            from libs.shared.extraction.ocr_label_extractor import (
                extract_fields_from_ocr_tokens,
            )

            label_tokens_by_page: dict[int, list[Any]] = {}
            for page_num in range(1, len(pages) + 1):
                # Respect active segment for composite documents
                if active_page_numbers and page_num not in active_page_numbers:
                    continue
                page_record = page_repo.get_page_with_tokens(job_uuid, page_num)
                if page_record and not page_record.is_cover_page:
                    if hasattr(page_record, "ocr_tokens") and page_record.ocr_tokens:
                        label_tokens_by_page[page_num] = page_record.ocr_tokens

            if label_tokens_by_page:
                label_results = extract_fields_from_ocr_tokens(
                    tokens_by_page=label_tokens_by_page,
                    payer_name=payer_str,
                )

                for field_key, label_candidate in label_results.items():
                    candidates_by_field.setdefault(field_key, []).append(
                        label_candidate
                    )
                    raw_candidates_by_field.setdefault(field_key, []).append(
                        label_candidate.to_dict()
                    )

                    # Only persist as primary if template didn't already extract this field
                    if field_key not in {
                        r.field_key for r in (extraction_results if template_verified else [])
                        if r.value
                    }:
                        field_repo.upsert_field(
                            fax_job_id=job_uuid,
                            field_key=field_key,
                            method=ExtractionMethodEnum.TEMPLATE_OCR,
                            field_value=label_candidate.value,
                            field_conf=label_candidate.confidence,
                            evidence_bbox=label_candidate.evidence_bbox,
                            evidence_text=label_candidate.evidence_text,
                        )

                logger.info(
                    "OCR label extraction: %d fields (%s)",
                    len(label_results),
                    "secondary" if template_verified else "primary",
                )

            # --- 9. LayoutLM Document QA extraction (gap filler) ---
            vlm_extraction_meta: dict[str, Any] | None = None

            # Collect non-cover page images + raw OCR token dicts for LayoutLM.
            content_page_images: list = []
            _raw_ocr_tokens: list[list[dict]] = []

            if settings.vlm.layoutlm_enabled:
                for page_num in range(1, len(pages) + 1):
                    page_record = page_repo.get_page_with_tokens(job_uuid, page_num)
                    if page_record and not page_record.is_cover_page:
                        content_page_images.append(pages[page_num - 1])
                        _page_toks: list[dict] = []
                        if hasattr(page_record, "ocr_tokens"):
                            for tok in page_record.ocr_tokens:
                                _page_toks.append({
                                    "text": tok.token_text,
                                    "bbox_x0": float(tok.bbox_x0),
                                    "bbox_y0": float(tok.bbox_y0),
                                    "bbox_x1": float(tok.bbox_x1),
                                    "bbox_y1": float(tok.bbox_y1),
                                    "confidence": float(tok.confidence),
                                })
                        _raw_ocr_tokens.append(_page_toks)

            # --- 9b. LayoutLM Document QA gap-fill ---
            layoutlm_extraction_meta: dict[str, Any] | None = None

            _LAYOUTLM_TIMEOUT_SECONDS = 90  # 90s — keeps total job < 5 min on CPU

            # Only query fields where template+OCR label extraction got nothing
            # or low confidence.  When template is at 100 %, LayoutLM asks zero
            # questions → fast even on CPU.
            _LM_CONF_THRESHOLD = 0.75
            try:
                from libs.shared.extraction.layoutlm_extractor import (
                    FIELD_QUESTIONS_KEYS as _LM_ALL_FIELDS,
                )
                _fields_for_layoutlm: list[str] = [
                    fk for fk in _LM_ALL_FIELDS
                    if max(
                        (c.confidence for c in candidates_by_field.get(fk, [])),
                        default=0.0,
                    ) < _LM_CONF_THRESHOLD
                ][:8]  # Cap at 8 fields max — prevents 10+ min CPU runs for unknown payers
            except Exception:
                _fields_for_layoutlm = []

            if settings.vlm.layoutlm_enabled and not content_page_images:
                logger.debug("LayoutLM skipped: no content page images for job %s", fax_job_id)
            elif settings.vlm.layoutlm_enabled and not _fields_for_layoutlm:
                logger.info(
                    "LayoutLM skipped: all fields already at conf >= %.2f for job %s",
                    _LM_CONF_THRESHOLD, fax_job_id,
                )

            if settings.vlm.layoutlm_enabled and content_page_images and _fields_for_layoutlm:
                import threading as _lm_threading

                lm_result_holder: list[Any] = [None]
                lm_error_holder: list[Any] = [None]

                def _run_layoutlm() -> None:
                    try:
                        from libs.shared.extraction.layoutlm_extractor import (
                            OcrTokenForAlignment,
                        )
                        # Use module-level singleton to avoid reloading model per task
                        lm_extractor = _get_layoutlm_extractor(settings)

                        # Convert raw OCR dicts → OcrTokenForAlignment
                        _lm_ocr_tokens: list[list[OcrTokenForAlignment]] = []
                        for page_toks in _raw_ocr_tokens:
                            _lm_ocr_tokens.append([
                                OcrTokenForAlignment(
                                    text=t["text"],
                                    x0=t["bbox_x0"], y0=t["bbox_y0"],
                                    x1=t["bbox_x1"], y1=t["bbox_y1"],
                                    confidence=t["confidence"],
                                )
                                for t in page_toks
                            ])

                        lm_result_holder[0] = lm_extractor.extract(
                            page_images=content_page_images,
                            ocr_tokens_by_page=_lm_ocr_tokens,
                            payer_name=payer_str,
                            fields_to_query=_fields_for_layoutlm,
                        )
                    except Exception as _e:
                        lm_error_holder[0] = _e

                lm_thread = _lm_threading.Thread(
                    target=_run_layoutlm, daemon=True, name="layoutlm-extract"
                )
                lm_thread.start()
                lm_thread.join(timeout=_LAYOUTLM_TIMEOUT_SECONDS)

                if lm_thread.is_alive():
                    logger.warning(
                        "LayoutLM extraction timed out after %ds for job %s; skipping",
                        _LAYOUTLM_TIMEOUT_SECONDS, fax_job_id,
                    )
                elif lm_error_holder[0] is not None:
                    logger.warning(
                        "LayoutLM extraction failed for job %s: %s; skipping",
                        fax_job_id, lm_error_holder[0],
                    )
                else:
                    lm_result = lm_result_holder[0]
                    if lm_result and lm_result.field_count > 0:
                        layoutlm_extraction_meta = lm_result.to_dict()

                        for field_key, lm_candidate in lm_result.fields.items():
                            candidates_by_field.setdefault(field_key, []).append(
                                lm_candidate
                            )
                            raw_candidates_by_field.setdefault(field_key, []).append(
                                lm_candidate.to_dict()
                            )

                            field_repo.upsert_field(
                                fax_job_id=job_uuid,
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

            # Add decision from doc classifier if not already present.
            # Use at least 0.80 confidence — the classifier's doc_type_conf
            # can be very low (0.10) even when correct, because it's a
            # keyword-based classifier, not a confidence-calibrated model.
            if decision_value and "decision" not in candidates_by_field:
                _decision_conf = max(0.80, float(job.doc_type_conf or 0.80))
                candidate = ExtractionCandidate(
                    value=decision_value,
                    method=ExtractionMethodEnum.TEMPLATE_OCR,
                    confidence=_decision_conf,
                )
                candidates_by_field["decision"] = [candidate]
                raw_candidates_by_field["decision"] = [candidate.to_dict()]

            # -------------------------------------------------------
            # 9c. Intelligent VLM pre-screening
            #     Demote LayoutLM candidates that are clearly wrong:
            #       - Same date repeated across 3+ date fields (confusion)
            #       - units_requested = long member_id number
            #       - Fax MSG# in auth/member fields
            # -------------------------------------------------------
            from libs.shared.extraction.field_builder import preprocess_vlm_candidates

            candidates_by_field = preprocess_vlm_candidates(candidates_by_field)

            # -------------------------------------------------------
            # 10. Multi-source merge via FieldBuilder
            # -------------------------------------------------------
            field_builder = FieldBuilder(
                vlm_multiplier=settings.vlm.layoutlm_score_multiplier,
            )
            extracted_fields: dict[str, dict[str, Any]] = {}

            for field_key, candidates in candidates_by_field.items():
                built = field_builder.build_field(field_key, candidates)
                extracted_fields[field_key] = {
                    "value": built.value,
                    "confidence": built.confidence,
                    "method": built.method.value,
                    "evidence_bbox": built.evidence_bbox,
                    "evidence_text": built.evidence_text,
                    "candidates": [c.to_dict() for c in built.candidates],
                }

            logger.info(
                "FieldBuilder merged %d fields from %d total candidates",
                len(extracted_fields),
                sum(len(c) for c in candidates_by_field.values()),
            )

            # -------------------------------------------------------
            # 10b. Smart post-extraction corrections
            #      Remove fields where final value is clearly wrong
            #      (agreement/dedup didn't fully resolve)
            # -------------------------------------------------------
            _member_id_val = (extracted_fields.get("member_id") or {}).get("value") or ""
            _dob_val = (extracted_fields.get("patient_dob") or {}).get("value") or ""
            _eff_val = (extracted_fields.get("auth_effective_date") or {}).get("value") or ""
            _exp_val = (extracted_fields.get("auth_expiration_date") or {}).get("value") or ""

            # units_requested that duplicates member_id → clear it
            _units_data = extracted_fields.get("units_requested")
            if _units_data and _member_id_val:
                _uval = (_units_data.get("value") or "").replace(" ", "")
                if _uval == _member_id_val.replace(" ", "") and len(_uval) >= 6:
                    logger.info(
                        "Auto-corrected units_requested: value '%s' duplicates member_id → cleared",
                        _uval,
                    )
                    extracted_fields.pop("units_requested", None)

            # next_review_date that equals patient_dob → it's DOB bleed, not a review date
            _review_data = extracted_fields.get("next_review_date")
            if _review_data and _dob_val:
                import re as _re_corr
                _normalize = lambda s: _re_corr.sub(r"\D", "", s)
                if _dob_val and _normalize(_review_data.get("value") or "") == _normalize(_dob_val):
                    logger.info(
                        "Auto-corrected next_review_date: value '%s' equals patient_dob → cleared",
                        _review_data.get("value"),
                    )
                    extracted_fields.pop("next_review_date", None)

            import re as _re_corr2
            _donly = lambda s: _re_corr2.sub(r"\D", "", s)

            # auth_effective_date == auth_expiration_date (both VLM) AND the shared date
            # also equals patient_dob or next_review_date → VLM confusion, clear both.
            # NOTE: Do NOT clear when both dates are legitimately the same (1-day authorization).
            _next_val = (extracted_fields.get("next_review_date") or {}).get("value") or ""
            if _eff_val and _exp_val and _eff_val == _exp_val:
                _eff_method = (extracted_fields.get("auth_effective_date") or {}).get("method", "")
                _exp_method = (extracted_fields.get("auth_expiration_date") or {}).get("method", "")
                _both_from_vlm = (
                    _eff_method not in ("TEMPLATE_OCR", "HYBRID") and
                    _exp_method not in ("TEMPLATE_OCR", "HYBRID")
                )
                # Only clear when the repeated value can be explained as bleeding from
                # another date field (= 3+ date fields all the same value)
                _also_matches_dob = _dob_val and _donly(_eff_val) == _donly(_dob_val)
                _also_matches_review = _next_val and _donly(_eff_val) == _donly(_next_val)
                if _both_from_vlm and (_also_matches_dob or _also_matches_review):
                    logger.info(
                        "Auto-corrected: auth_effective_date and auth_expiration_date both from "
                        "VLM with same value '%s' — matches another date field, likely confusion; cleared",
                        _eff_val,
                    )
                    extracted_fields.pop("auth_effective_date", None)
                    extracted_fields.pop("auth_expiration_date", None)

            # Refresh after potential pops above
            _eff_val = (extracted_fields.get("auth_effective_date") or {}).get("value") or ""
            _exp_val = (extracted_fields.get("auth_expiration_date") or {}).get("value") or ""
            _dob_val = (extracted_fields.get("patient_dob") or {}).get("value") or ""

            # auth_expiration_date == patient_dob → VLM confused DOB for expiry.
            # Try to recover from template candidate before clearing.
            if _exp_val and _dob_val and _donly(_exp_val) == _donly(_dob_val):
                _exp_data = extracted_fields.get("auth_expiration_date") or {}
                _alt_exp = None
                for _cand in (_exp_data.get("candidates") or []):
                    _cval = _cand.get("value") or ""
                    if _cval and _donly(_cval) != _donly(_dob_val):
                        _alt_exp = _cand
                        break
                if _alt_exp:
                    logger.info(
                        "auth_expiration_date was DOB bleed ('%s'); recovered template candidate '%s'",
                        _exp_val, _alt_exp.get("value"),
                    )
                    extracted_fields["auth_expiration_date"] = {
                        "value": _alt_exp["value"],
                        "confidence": float(_alt_exp.get("confidence") or 0.70),
                        "method": _alt_exp.get("method", "TEMPLATE_OCR"),
                        "evidence_bbox": _alt_exp.get("evidence_bbox"),
                        "evidence_text": _alt_exp.get("evidence_text"),
                        "candidates": [],
                    }
                else:
                    logger.info(
                        "Auto-corrected auth_expiration_date: value '%s' equals patient_dob → cleared",
                        _exp_val,
                    )
                    extracted_fields.pop("auth_expiration_date", None)

            # auth_effective_date == patient_dob → same confusion, clear it
            if _eff_val and _dob_val and _donly(_eff_val) == _donly(_dob_val):
                logger.info(
                    "Auto-corrected auth_effective_date: value '%s' equals patient_dob → cleared",
                    _eff_val,
                )
                extracted_fields.pop("auth_effective_date", None)

            # patient_dob: impossible birth year (> 2015) → clearly not a real DOB for adult patient
            _dob_data = extracted_fields.get("patient_dob")
            if _dob_data:
                _dob_yr_m = _re_corr2.search(r"\b(20\d{2}|19\d{2}|18\d{2})\b",
                                              _dob_data.get("value") or "")
                if _dob_yr_m:
                    _dob_year = int(_dob_yr_m.group(1))
                    if _dob_year > 2015:  # Not a valid adult patient DOB
                        logger.info(
                            "Auto-corrected patient_dob: year %d > 2015 is invalid → cleared",
                            _dob_year,
                        )
                        extracted_fields.pop("patient_dob", None)

            # units_requested: must be a simple integer (e.g. "1", "30") not prose/date-like
            _units_data2 = extracted_fields.get("units_requested")
            if _units_data2:
                _uval2 = (_units_data2.get("value") or "").strip()
                # Invalid: empty, contains spaces, non-digits, or out of range 1-9999
                _units_invalid = (
                    not _uval2 or
                    not _re_corr2.match(r"^\d{1,4}$", _uval2) or
                    not (1 <= int(_uval2) <= 9999)
                )
                if _units_invalid:
                    logger.info(
                        "Auto-corrected units_requested: value '%s' is not a valid unit count → cleared",
                        _uval2,
                    )
                    extracted_fields.pop("units_requested", None)

            # patient_name = provider contamination: if all name-words of patient_name exist
            # in provider_name (case-insensitive), the VLM likely extracted provider info
            _pn_data = extracted_fields.get("patient_name")
            _prvn_data = extracted_fields.get("provider_name")
            if _pn_data and _prvn_data:
                _pn_val = (_pn_data.get("value") or "").lower()
                _prvn_val = (_prvn_data.get("value") or "").lower()
                _pn_words = set(_re_corr2.sub(r"[^a-z]", " ", _pn_val).split())
                _prvn_words = set(_re_corr2.sub(r"[^a-z]", " ", _prvn_val).split())
                if _pn_words and _prvn_words and _pn_words.issubset(_prvn_words):
                    _pn_method = _pn_data.get("method", "")
                    # Only clear if patient_name is NOT from template (template usually has real patient)
                    if _pn_method not in ("TEMPLATE_OCR",):
                        logger.info(
                            "Auto-corrected patient_name: value '%s' subset of provider_name '%s' → cleared",
                            _pn_val, _prvn_val,
                        )
                        extracted_fields.pop("patient_name", None)

            # When template is verified AND patient_name came from VLM only, prefer template candidate
            _pn_data2 = extracted_fields.get("patient_name")
            if template_verified and _pn_data2 and _pn_data2.get("method") == "LAYOUTLM":
                _pn_candidates = _pn_data2.get("candidates") or []
                for _cand in _pn_candidates:
                    if _cand.get("method") == "TEMPLATE_OCR" and _cand.get("value"):
                        logger.info(
                            "patient_name: VLM='%s', promoting template candidate '%s' (template verified)",
                            _pn_data2.get("value"), _cand["value"],
                        )
                        extracted_fields["patient_name"] = {
                            "value": _cand["value"],
                            "confidence": float(_cand.get("confidence") or 0.75),
                            "method": "TEMPLATE_OCR",
                            "evidence_bbox": _cand.get("evidence_bbox"),
                            "evidence_text": _cand.get("evidence_text"),
                            "candidates": [],
                        }
                        break

            # -------------------------------------------------------
            # 10c. Service code fallback: scan full OCR text for HCPCS/CPT
            #      codes when template/label extraction missed it.
            #      Many faxes embed codes in prose ("G0480 x1 Drug Test").
            # -------------------------------------------------------
            if not (extracted_fields.get("service_code") or {}).get("value"):
                import re as _re_sc
                _HCPCS_PAT = _re_sc.compile(r"\b([A-Z]\d{4}[A-Z]?)\b")
                # Build scan text from all_ocr_text PLUS raw OCR token dicts
                # (_raw_ocr_tokens is always defined when any VLM is enabled)
                _extra_token_text = " ".join(
                    t["text"]
                    for _pt in _raw_ocr_tokens
                    for t in _pt
                    if t.get("text")
                )
                _scan_text = (all_ocr_text or "") + " " + _extra_token_text
                _hcpcs_candidates: list[str] = []
                for _match in _HCPCS_PAT.finditer(_scan_text):
                    _code = _match.group(1)
                    # Exclude false positives: known NPI/ID patterns, page numbers
                    if len(_code) >= 5 and not _code.startswith(("19", "18", "20")):
                        _hcpcs_candidates.append(_code)
                # Deduplicate preserving first occurrence
                _seen_codes: set[str] = set()
                _unique_codes: list[str] = []
                for _c in _hcpcs_candidates:
                    if _c not in _seen_codes:
                        _seen_codes.add(_c)
                        _unique_codes.append(_c)
                if _unique_codes:
                    _code_val = _unique_codes[0]
                    logger.info(
                        "Service code fallback scanner found: %s (from %d candidates)",
                        _code_val, len(_unique_codes),
                    )
                    extracted_fields["service_code"] = {
                        "value": _code_val,
                        "confidence": 0.65,
                        "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                        "evidence_bbox": None,
                        "evidence_text": f"OCR scan: {' '.join(_unique_codes[:3])}",
                        "candidates": [],
                    }

            # -------------------------------------------------------
            # 10d. Targeted OCR pattern scanners: recover fields that
            #      VLM missed or got wrong, using direct text matching.
            # -------------------------------------------------------
            import re as _re10d

            # Use full pre-composite OCR text so recovery scans can find
            # content from pages outside the active composite segment.
            _scan_ocr = _full_doc_ocr_text or all_ocr_text or ""

            # --- Auth date range recovery ---
            # If auth_effective_date is known but auth_expiration_date is missing,
            # look for date ranges like "12/29/2022-12/31/2022" in OCR text.
            _eff_now = (extracted_fields.get("auth_effective_date") or {}).get("value") or ""
            _exp_now = (extracted_fields.get("auth_expiration_date") or {}).get("value") or ""
            if _eff_now and not _exp_now:
                _eff_dig = _re10d.sub(r"\D", "", _eff_now)
                _range_re = _re10d.compile(
                    r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s*[-–]+\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})"
                )
                for _rm in _range_re.finditer(_scan_ocr):
                    if _re10d.sub(r"\D", "", _rm.group(1)) == _eff_dig:
                        _end_d = _rm.group(2)
                        logger.info(
                            "Auth date range recovery: effective=%s, found expiration=%s",
                            _eff_now, _end_d,
                        )
                        extracted_fields["auth_expiration_date"] = {
                            "value": _end_d,
                            "confidence": 0.70,
                            "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                            "evidence_bbox": None,
                            "evidence_text": f"OCR range: {_rm.group(0)}",
                            "candidates": [],
                        }
                        break

            # --- Service dates scanner (Buckeye / some Molina docs) ---
            # "Service dates: 06/07/2023 - 06/07/2023" → auth_effective + auth_expiration
            if not (extracted_fields.get("auth_effective_date") or {}).get("value"):
                _sd_re = _re10d.compile(
                    r"(?:Service|Authorization)\s+[Dd]ates?\s*:?\s*"
                    r"(\d{1,2}/\d{1,2}/\d{4})\s*[-–]+\s*(\d{1,2}/\d{1,2}/\d{4})",
                    _re10d.IGNORECASE,
                )
                _sd_m = _sd_re.search(_scan_ocr)
                if _sd_m:
                    logger.info(
                        "Service dates scanner found: %s - %s",
                        _sd_m.group(1), _sd_m.group(2),
                    )
                    extracted_fields["auth_effective_date"] = {
                        "value": _sd_m.group(1),
                        "confidence": 0.65,
                        "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                        "evidence_bbox": None,
                        "evidence_text": f"OCR: {_sd_m.group(0)}",
                        "candidates": [],
                    }
                    if not (extracted_fields.get("auth_expiration_date") or {}).get("value"):
                        extracted_fields["auth_expiration_date"] = {
                            "value": _sd_m.group(2),
                            "confidence": 0.65,
                            "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                            "evidence_bbox": None,
                            "evidence_text": f"OCR: {_sd_m.group(0)}",
                            "candidates": [],
                        }

            # --- Visits/units scanner (Buckeye narrative format) ---
            if not (extracted_fields.get("units_requested") or {}).get("value"):
                _units_re = _re10d.compile(
                    r"(?:units?|visits?)\s+(?:authorized|approved|requested)\s*:?\s*(\d+)",
                    _re10d.IGNORECASE,
                )
                _units_m = _units_re.search(_scan_ocr)
                if _units_m:
                    _unit_val = _units_m.group(1)
                    if 1 <= int(_unit_val) <= 9999:
                        logger.info("Units scanner found: %s", _unit_val)
                        extracted_fields["units_requested"] = {
                            "value": _unit_val,
                            "confidence": 0.65,
                            "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                            "evidence_bbox": None,
                            "evidence_text": f"OCR: {_units_m.group(0)}",
                            "candidates": [],
                        }

            # --- prior_auth_number: validate format, recover from OCR scan or candidates ---
            _auth_data10d = extracted_fields.get("prior_auth_number")
            if _auth_data10d:
                _av10d = (_auth_data10d.get("value") or "").strip()
                _av10d_method = (_auth_data10d.get("method") or "")
                _av10d_conf = float(_auth_data10d.get("confidence") or 0)
                # Valid auth numbers have no spaces and ≥ 5 alphanumeric chars.
                # Flag low-confidence VLM values containing hyphens
                # (e.g. fax MSG# "1846555098-008-1" misidentified as auth#).
                _av10d_hyphen_junk = (
                    "-" in _av10d
                    and _av10d_conf < 0.5
                    and _av10d_method == "LAYOUTLM"
                )
                if " " in _av10d or len(_av10d) < 5 or _av10d_hyphen_junk:
                    logger.info(
                        "prior_auth_number '%s' has spaces/too short — invalid, trying recovery",
                        _av10d,
                    )
                    # Try candidates list first
                    _alt_auth = None
                    for _cand in (_auth_data10d.get("candidates") or []):
                        _cv = (_cand.get("value") or "").strip()
                        if _cv and " " not in _cv and "-" not in _cv and len(_cv) >= 5 and _cv != _av10d:
                            _alt_auth = _cand
                            break
                    if _alt_auth:
                        logger.info(
                            "prior_auth_number recovered from candidate: %s",
                            _alt_auth.get("value"),
                        )
                        extracted_fields["prior_auth_number"] = {
                            "value": _alt_auth["value"],
                            "confidence": float(_alt_auth.get("confidence") or 0.65),
                            "method": _alt_auth.get("method", "TEMPLATE_OCR"),
                            "evidence_bbox": _alt_auth.get("evidence_bbox"),
                            "evidence_text": _alt_auth.get("evidence_text"),
                            "candidates": [],
                        }
                    else:
                        # Try OCR scan for Reference# pattern (Molina)
                        _ref_re = _re10d.compile(
                            r"Reference\s*#\s*:?\s*([A-Za-z0-9]{5,25})", _re10d.IGNORECASE
                        )
                        _ref_m = _ref_re.search(_scan_ocr)
                        if _ref_m:
                            logger.info(
                                "prior_auth_number Reference# scan found: %s", _ref_m.group(1),
                            )
                            extracted_fields["prior_auth_number"] = {
                                "value": _ref_m.group(1),
                                "confidence": 0.65,
                                "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                                "evidence_bbox": None,
                                "evidence_text": f"OCR: {_ref_m.group(0)}",
                                "candidates": [],
                            }
                        else:
                            logger.info("prior_auth_number cleared (no valid candidate found)")
                            extracted_fields.pop("prior_auth_number", None)

            # --- Patient name: recover from candidates after contamination clearing ---
            # _pn_data (set earlier) still holds pre-clearing candidate list
            if not (extracted_fields.get("patient_name") or {}).get("value"):
                _prvn_val_now = (
                    (extracted_fields.get("provider_name") or {}).get("value") or ""
                ).lower()
                _prvn_words_now = set(
                    _re10d.sub(r"[^a-z]", " ", _prvn_val_now).split()
                )
                # Try candidates from the previously cleared patient_name dict
                _pn_cand_source = _pn_data if _pn_data else None
                if not _pn_cand_source:
                    _pn_cand_source = _pn_data2 if _pn_data2 else None
                for _cand in ((_pn_cand_source or {}).get("candidates") or []):
                    _cv = (_cand.get("value") or "").strip()
                    if not _cv:
                        continue
                    _cv_words = set(_re10d.sub(r"[^a-z]", " ", _cv.lower()).split())
                    # Skip if it's the same contaminated value or subset of provider name
                    if _cv_words and _prvn_words_now and _cv_words.issubset(_prvn_words_now):
                        continue
                    if _cv == (_pn_data or {}).get("value"):
                        continue  # Same contaminated value
                    logger.info(
                        "patient_name recovered from candidate after contamination clear: %s", _cv
                    )
                    extracted_fields["patient_name"] = {
                        "value": _cv,
                        "confidence": float(_cand.get("confidence") or 0.60),
                        "method": _cand.get("method", "TEMPLATE_OCR"),
                        "evidence_bbox": _cand.get("evidence_bbox"),
                        "evidence_text": _cand.get("evidence_text"),
                        "candidates": [],
                    }
                    break

            # Also try Member Name: scan from OCR text (for Molina)
            if not (extracted_fields.get("patient_name") or {}).get("value"):
                _mn_label_re = _re10d.compile(
                    r"Member\s+Name\s*:?\s*(.+)", _re10d.IGNORECASE
                )
                for _mn_line in _scan_ocr.split("\n"):
                    _mn_m = _mn_label_re.match(_mn_line.strip())
                    if _mn_m:
                        _mn_val = _mn_m.group(1).strip()
                        # Strip known label keywords that follow inline on same line
                        # e.g. "MAYNARD AMANDA Requesting Provider: BOVA, DAWN" → "MAYNARD AMANDA"
                        _mn_val = _re10d.sub(
                            r"\s+(?:Requesting|Servicing|Ordering|Attending|Referring|Primary)\s+Provider.*$",
                            "", _mn_val, flags=_re10d.IGNORECASE
                        ).strip()
                        # Strip trailing digits/date/phone (e.g. "MAYNARD AMANDA 3/1/90" → "MAYNARD AMANDA")
                        _mn_val = _re10d.sub(r"\s*\d.*$", "", _mn_val).strip()
                        _mn_val = _mn_val.strip(",").strip()
                        if len(_mn_val) >= 3 and any(c.isalpha() for c in _mn_val):
                            logger.info("Member Name OCR scan found patient_name: %s", _mn_val)
                            extracted_fields["patient_name"] = {
                                "value": _mn_val,
                                "confidence": 0.60,
                                "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                                "evidence_bbox": None,
                                "evidence_text": f"OCR: {_mn_line.strip()}",
                                "candidates": [],
                            }
                            break

            # "Following member" scan: Buckeye/payer letter format has patient name
            # on the line immediately after "following member:".
            # This overrides LAYOUTLM when it picks up the fax cover recipient instead.
            _fm_re = _re10d.compile(
                r"following\s+member\s*:[ \t]*\n\s*([^\n]+)", _re10d.IGNORECASE
            )
            _fm_m = _fm_re.search(_scan_ocr)
            if _fm_m:
                _fm_name = _fm_m.group(1).strip().rstrip(",").strip()
                _fm_name = _re10d.sub(r"\s*\d.*$", "", _fm_name).strip()  # strip trailing date
                _pn_current = (extracted_fields.get("patient_name") or {}).get("value") or ""
                _pn_conf = float((extracted_fields.get("patient_name") or {}).get("confidence") or 0)
                if len(_fm_name) >= 3 and any(c.isalpha() for c in _fm_name):
                    if not _pn_current or _pn_current.upper() != _fm_name.upper():
                        logger.info(
                            "patient_name 'following member' scan overrides '%s' → '%s'",
                            _pn_current, _fm_name,
                        )
                        extracted_fields["patient_name"] = {
                            "value": _fm_name,
                            "confidence": 0.85,
                            "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                            "evidence_bbox": None,
                            "evidence_text": f"OCR: following member: {_fm_name}",
                            "candidates": [],
                        }

            # next_review_date → patient_dob transfer: if next_review_date contains
            # a date that is 18+ years in the past it is a birth date, not a review date.
            import datetime as _dt10d
            if not (extracted_fields.get("patient_dob") or {}).get("value"):
                _nrd_data = extracted_fields.get("next_review_date") or {}
                _nrd_val = (_nrd_data.get("value") or "").strip()
                if _nrd_val:
                    _nrd_parsed = None
                    for _nrd_fmt in ["%m/%d/%Y", "%m/%d/%y"]:
                        try:
                            _nrd_parsed = _dt10d.datetime.strptime(_nrd_val, _nrd_fmt)
                            break
                        except Exception:
                            pass
                    if _nrd_parsed is None:
                        # Try normalising slashes / dashes then parse again
                        _nrd_norm = _re10d.sub(r"[-.]", "/", _nrd_val)
                        for _nrd_fmt in ["%m/%d/%Y", "%m/%d/%y"]:
                            try:
                                _nrd_parsed = _dt10d.datetime.strptime(_nrd_norm, _nrd_fmt)
                                break
                            except Exception:
                                pass
                    if _nrd_parsed:
                        _years_ago = (_dt10d.datetime.now() - _nrd_parsed).days / 365.25
                        if _years_ago >= 18:
                            logger.info(
                                "next_review_date '%s' is %d years in the past → transferring to patient_dob",
                                _nrd_val, int(_years_ago),
                            )
                            extracted_fields["patient_dob"] = {
                                "value": _nrd_val,
                                "confidence": 0.65,
                                "method": _nrd_data.get("method", ExtractionMethodEnum.TEMPLATE_OCR.value),
                                "evidence_bbox": _nrd_data.get("evidence_bbox"),
                                "evidence_text": _nrd_data.get("evidence_text", f"OCR: {_nrd_val}"),
                                "candidates": [],
                            }
                            extracted_fields.pop("next_review_date", None)

            # Update payer_str in case template match changed detected_payer
            payer_str = (
                detected_payer.value
                if detected_payer != PayerNameEnum.UNKNOWN
                else None
            )

            # -------------------------------------------------------
            # 11. Field validation + canonicalization
            # -------------------------------------------------------
            from libs.shared.extraction.canonicalizer import FieldCanonicalizer
            from libs.shared.extraction.validators import FieldValidator

            validator = FieldValidator()
            canonicalizer = FieldCanonicalizer()

            for field_key, field_data in extracted_fields.items():
                value = field_data.get("value")
                if not value:
                    continue

                # Validate
                val_result = validator.validate(
                    field_key=field_key,
                    value=value,
                    payer_name=payer_str,
                    context={k: v.get("value") for k, v in extracted_fields.items()},
                )
                field_data["validation_passed"] = val_result.is_valid
                field_data["validation_errors"] = val_result.errors

                # Canonicalize
                field_type = validator._infer_field_type(field_key)
                canonical_value = canonicalizer.canonicalize(
                    value, field_type=field_type, payer_name=payer_str
                )
                if canonical_value and canonical_value != value:
                    field_data["canonical_value"] = canonical_value

                # Update DB record
                field_repo.update_validation(
                    fax_job_id=job_uuid,
                    field_key=field_key,
                    validation_passed=val_result.is_valid,
                    validation_errors=val_result.errors,
                )

            # -------------------------------------------------------
            # 13. Cross-field consistency checks
            # -------------------------------------------------------
            from libs.shared.extraction.cross_field_validator import CrossFieldValidator

            cross_validator = CrossFieldValidator()
            cross_result = cross_validator.validate(extracted_fields)

            if not cross_result.is_consistent:
                logger.warning(
                    "Cross-field inconsistencies: %s", cross_result.errors
                )

            # -------------------------------------------------------
            # 14. Weighted confidence scoring (with OCR quality)
            # -------------------------------------------------------
            # Compute OCR quality metrics from page records
            ocr_quality: dict[str, float] | None = None
            blur_scores = []
            text_densities = []
            token_confidences = []
            for page_num in range(1, len(pages) + 1):
                if active_page_numbers and page_num not in active_page_numbers:
                    continue
                pr = page_repo.get_page_with_tokens(job_uuid, page_num)
                if pr and not pr.is_cover_page:
                    if pr.blur_score is not None:
                        blur_scores.append(float(pr.blur_score))
                    if pr.text_density is not None:
                        text_densities.append(float(pr.text_density))
                    if hasattr(pr, "ocr_tokens") and pr.ocr_tokens:
                        avg_conf = sum(
                            float(t.confidence) for t in pr.ocr_tokens
                        ) / len(pr.ocr_tokens)
                        token_confidences.append(avg_conf)

            if blur_scores or text_densities or token_confidences:
                ocr_quality = {}
                if blur_scores:
                    ocr_quality["avg_blur_score"] = sum(blur_scores) / len(blur_scores)
                if text_densities:
                    ocr_quality["avg_text_density"] = sum(text_densities) / len(text_densities)
                if token_confidences:
                    ocr_quality["avg_token_confidence"] = sum(token_confidences) / len(token_confidences)

            scorer = ConfidenceScorer()
            scoring_result = scorer.score(
                fields=extracted_fields,
                payer_name=payer_str,
                candidates_by_field=raw_candidates_by_field,
                ocr_quality=ocr_quality,
            )
            overall_conf = scoring_result.overall_confidence
            job.overall_conf = overall_conf

            # -------------------------------------------------------
            # 15. Determine if review needed
            # -------------------------------------------------------
            # When template match score is high (>= 0.85), skip the VLM
            # agree-to-finalize check — template extraction is trusted.
            _match_score = match_result.score if (match_result and match_result.matched) else 0.0
            _candidates_for_review = (
                raw_candidates_by_field if _match_score < 0.85 else None
            )
            needs_review = scorer.determine_needs_review(
                scoring_result=scoring_result,
                auto_finalize_threshold=settings.confidence.auto_finalize,
                payer_name=payer_str,
                candidates_by_field=_candidates_for_review,
            )

            # DEBUG: trace review decision
            logger.info(
                "REVIEW_DEBUG job=%s payer=%s conf=%.3f threshold=%.2f "
                "needs_review=%s critical_complete=%s missing_critical=%d "
                "val_failures=%d match_score=%.2f reasons=%s",
                str(job_uuid)[:8], payer_str, overall_conf,
                settings.confidence.auto_finalize, needs_review,
                scoring_result.critical_fields_complete,
                scoring_result.missing_critical_count,
                scoring_result.validation_failure_count,
                _match_score, scoring_result.review_reasons,
            )

            # Also flag for review if cross-field checks failed
            if not cross_result.is_consistent:
                needs_review = True
                scoring_result.review_reasons.append("CROSS_FIELD_INCONSISTENCY")

            # Flag unknown payer/doc type
            if detected_payer == PayerNameEnum.UNKNOWN:
                needs_review = True
                scoring_result.review_reasons.append("UNKNOWN_PAYER")
            if job.doc_type == DocTypeEnum.UNKNOWN:
                needs_review = True
                scoring_result.review_reasons.append("UNKNOWN_DOC_TYPE")

            job.needs_review = needs_review

            # -------------------------------------------------------
            # 16. Store final extraction + metadata
            # -------------------------------------------------------
            vlm_models = []
            if layoutlm_extraction_meta:
                vlm_models.append("layoutlm-document-qa")

            model_versions: dict[str, str] = {
                "ocr": "paddle-pp-ocrv5",
                "vlm": "+".join(vlm_models) if vlm_models else "none",
                "classifier": "stub-v1",
                "embedder": "all-MiniLM-L6-v2",
                "pipeline": "3.0.0",
            }

            extraction_repo = ExtractionRepository(db)
            extraction_repo.upsert(
                fax_job_id=job_uuid,
                extraction_json=extracted_fields,
                model_versions=model_versions,
                pipeline_version="3.0.0",
            )

            # -------------------------------------------------------
            # 16b. HITL — compute per-field confidence flags
            # -------------------------------------------------------
            hitl_flags: list[dict] = []
            if settings.hitl.enabled:
                from libs.shared.extraction.hitl import compute_field_flags

                hitl_flags = compute_field_flags(
                    extracted_fields,
                    payer_name=detected_payer.value if detected_payer else None,
                    default_threshold=settings.hitl.default_threshold,
                    critical_threshold=settings.hitl.critical_threshold,
                )
                if hitl_flags:
                    extraction_repo.update_flagged_fields(job_uuid, hitl_flags)
                    logger.info(
                        "HITL step 16b: %d field(s) flagged for review [job=%s]",
                        len(hitl_flags),
                        str(job_uuid)[:8],
                    )
                    # Force NEEDS_REVIEW if enough critical flags are present
                    if (
                        not needs_review
                        and len(hitl_flags) >= settings.hitl.min_flags_for_review
                    ):
                        needs_review = True
                        scoring_result.review_reasons.append("HITL_LOW_CONFIDENCE_FIELDS")
                        logger.info(
                            "HITL: overriding to NEEDS_REVIEW (%d flag(s)) [job=%s]",
                            len(hitl_flags),
                            str(job_uuid)[:8],
                        )

            # Store detection / classification metadata on the job
            job.job_metadata = {
                **(job.job_metadata or {}),
                "payer_detection": payer_detection_meta,
                "doc_classification": doc_class_meta,
                "document_splitting": split_meta,
                "vlm_extraction": (
                    {
                        "model": vlm_extraction_meta.get("model_name") if vlm_extraction_meta else None,
                        "latency_ms": vlm_extraction_meta.get("latency_ms") if vlm_extraction_meta else None,
                        "field_count": vlm_extraction_meta.get("field_count") if vlm_extraction_meta else None,
                    }
                    if vlm_extraction_meta
                    else None
                ),
                "layoutlm_extraction": (
                    {
                        "model": layoutlm_extraction_meta.get("model_name") if layoutlm_extraction_meta else None,
                        "latency_ms": layoutlm_extraction_meta.get("latency_ms") if layoutlm_extraction_meta else None,
                        "field_count": layoutlm_extraction_meta.get("field_count") if layoutlm_extraction_meta else None,
                    }
                    if layoutlm_extraction_meta
                    else None
                ),
                "cross_field_validation": cross_result.to_dict(),
                "confidence_scoring": scoring_result.to_dict(),
            }

            # -------------------------------------------------------
            # 16-17. Create review or finalize
            # -------------------------------------------------------
            if needs_review:
                job.status = FaxJobStatusEnum.NEEDS_REVIEW
                review_repo = ReviewRepository(db)

                review_packet = {
                    "fax_job_id": str(job_uuid),
                    "pages": [
                        {"page_number": p, "page_id": str(page_id_map[p])}
                        for p in page_id_map
                    ],
                    "extracted_fields": extracted_fields,
                    "template_match": match_result.to_dict() if match_result else None,
                    "payer_detection": payer_detection_meta,
                    "doc_classification": doc_class_meta,
                    "flagged_fields": hitl_flags,
                }

                review = FaxReview(
                    fax_job_id=job_uuid,
                    review_packet=review_packet,
                    review_reasons=scoring_result.review_reasons[:8],
                )
                review_repo.create(review)

                # Call TaskClient stub to create review task
                from libs.shared.clients.task_client import TaskClient

                task_client = TaskClient()
                task_client.create_review_task(
                    fax_job_id=str(job_uuid),
                    reason_codes=scoring_result.review_reasons[:8],
                )
            else:
                job.status = FaxJobStatusEnum.COMPLETED

                # Call PriorAuthClient stub to attach extraction
                from libs.shared.clients.prior_auth_client import PriorAuthClient

                prior_auth_client = PriorAuthClient()
                prior_auth_client.attach_extraction(
                    case_id=job.external_fax_id or str(job_uuid),
                    extraction_json=extracted_fields,
                )

            job.processing_completed_at = datetime.now(timezone.utc)
            db.commit()

            # -------------------------------------------------------
            # Finalize pipeline metrics
            # -------------------------------------------------------
            metrics.total_duration_ms = (
                _time.perf_counter() - pipeline_start
            ) * 1000
            metrics.total_pages = len(pages)
            metrics.payer_detected = detected_payer.value
            metrics.payer_confidence = (
                payer_detection_meta.get("confidence", 0.0)
                if payer_detection_meta
                else 0.0
            )
            metrics.doc_type_detected = (
                job.doc_type.value if job.doc_type else "UNKNOWN"
            )
            metrics.doc_type_confidence = float(job.doc_type_conf or 0.0)
            metrics.template_matched = (
                match_result.matched if match_result else False
            )
            metrics.template_name = (
                match_result.template_name if match_result else None
            )
            metrics.template_match_score = (
                match_result.score if match_result else 0.0
            )
            metrics.template_match_page = (
                match_result.matched_page_number if match_result else None
            )
            metrics.template_verified = template_verified
            metrics.is_composite = split_meta.get("is_composite", False) if split_meta else False
            metrics.segment_count = split_meta.get("segment_count", 1) if split_meta else 1
            metrics.total_fields_extracted = len(extracted_fields)
            metrics.template_fields_extracted = sum(
                1 for r in (extraction_results if template_verified else [])
                if r.value
            )
            metrics.overall_confidence = overall_conf
            metrics.needs_review = needs_review
            metrics.review_reasons = scoring_result.review_reasons
            metrics.final_status = job.status.value
            if ocr_quality:
                metrics.avg_ocr_confidence = ocr_quality.get(
                    "avg_token_confidence", 0.0
                )
                metrics.avg_blur_score = ocr_quality.get(
                    "avg_blur_score", 0.0
                )

            # Store metrics in job metadata
            job.job_metadata = {
                **(job.job_metadata or {}),
                "pipeline_metrics": metrics.to_dict(),
            }
            db.commit()

            # Log structured summary
            metrics.log_summary()

            return {
                "fax_job_id": fax_job_id,
                "status": job.status.value,
                "pages": len(pages),
                "payer": detected_payer.value,
                "doc_type": job.doc_type.value if job.doc_type else None,
                "template_matched": match_result.matched if match_result else False,
                "template_fields": sum(1 for c in candidates_by_field.values() if any(
                    x.method == ExtractionMethodEnum.TEMPLATE_OCR for x in c
                )),
                "vlm_fields": vlm_extraction_meta.get("field_count", 0) if vlm_extraction_meta else 0,
                "overall_confidence": overall_conf,
                "needs_review": needs_review,
                "fields_extracted": len(extracted_fields),
                "validation_failures": scoring_result.validation_failure_count,
                "cross_field_consistent": cross_result.is_consistent,
                "review_reasons": scoring_result.review_reasons,
            }

        except Exception as e:
            job_repo.mark_failed(job_uuid, str(e))
            db.commit()
            raise


# -----------------------------------------------------------------------
# Page splitters (unchanged from Week 1)
# -----------------------------------------------------------------------

# Maximum pages to process (prevents memory exhaustion on huge faxes)
MAX_PAGES = 100


def split_to_pages(file_bytes: bytes, filename: str) -> list[np.ndarray]:
    """
    Split a file into page images.

    Supports PDF, TIFF, and single images.  Enforces ``MAX_PAGES``
    limit to prevent memory exhaustion on extremely large documents.

    Args:
        file_bytes: File content.
        filename: Original filename for type detection.

    Returns:
        List of page images as numpy arrays.

    Raises:
        ValueError: If the file is password-protected or exceeds MAX_PAGES.
    """
    filename_lower = filename.lower()

    if filename_lower.endswith(".pdf"):
        pages = split_pdf_to_pages(file_bytes)
    elif filename_lower.endswith((".tif", ".tiff")):
        pages = split_tiff_to_pages(file_bytes)
    else:
        pages = [load_image(file_bytes)]

    if len(pages) > MAX_PAGES:
        logger.warning(
            "Document has %d pages, truncating to %d", len(pages), MAX_PAGES
        )
        pages = pages[:MAX_PAGES]

    return pages


def split_pdf_to_pages(file_bytes: bytes) -> list[np.ndarray]:
    """Split PDF to page images.

    Raises:
        ValueError: If the PDF is password-protected or encrypted.
    """
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=file_bytes, filetype="pdf")

        # Check for encryption / password protection
        if doc.is_encrypted:
            doc.close()
            raise ValueError(
                "Password-protected or encrypted PDF. "
                "Please provide an unencrypted version."
            )

        pages = []

        for page_num in range(len(doc)):
            page = doc[page_num]
            mat = fitz.Matrix(300 / 72, 300 / 72)
            pix = page.get_pixmap(matrix=mat)

            img = np.frombuffer(pix.samples, dtype=np.uint8)
            img = img.reshape(pix.height, pix.width, pix.n)

            if pix.n == 4:
                img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
            elif pix.n == 3:
                img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

            pages.append(img)

        doc.close()
        return pages

    except ImportError:
        from pdf2image import convert_from_bytes

        pil_images = convert_from_bytes(file_bytes, dpi=300)
        return [np.array(img)[:, :, ::-1] for img in pil_images]


def split_tiff_to_pages(file_bytes: bytes) -> list[np.ndarray]:
    """Split multi-page TIFF to page images."""
    img = Image.open(io.BytesIO(file_bytes))
    pages = []

    try:
        while True:
            page = np.array(img)
            if len(page.shape) == 2:
                page = cv2.cvtColor(page, cv2.COLOR_GRAY2BGR)
            elif page.shape[2] == 4:
                page = cv2.cvtColor(page, cv2.COLOR_RGBA2BGR)
            elif page.shape[2] == 3:
                page = cv2.cvtColor(page, cv2.COLOR_RGB2BGR)

            pages.append(page)
            img.seek(img.tell() + 1)
    except EOFError:
        pass

    return pages


def _rotate_image(image: np.ndarray, angle: int) -> np.ndarray:
    """Rotate an image by 90, 180, or 270 degrees."""
    if angle == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    elif angle == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    elif angle == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return image


def load_image(file_bytes: bytes) -> np.ndarray:
    """Load a single image."""
    nparr = np.frombuffer(file_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        pil_img = Image.open(io.BytesIO(file_bytes))
        img = np.array(pil_img)
        if len(img.shape) == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        elif img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
        elif img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    return img
