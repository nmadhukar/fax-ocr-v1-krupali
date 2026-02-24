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

Pipeline logic is delegated to stage modules in
``workers.fax_processing_worker.tasks.stages``.
"""

import io
import logging
import time
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
from libs.shared.monitoring.pipeline_metrics import PipelineMetrics
from libs.shared.ocr.paddle_client import PaddleOcrClient
from libs.shared.scoring.confidence_scorer import ConfidenceScorer
from libs.shared.storage.s3_adapter import S3StorageAdapter
from libs.shared.template.matcher import TemplateMatcher
from libs.shared.utils.image_utils import ImagePreprocessor

# Imports previously inline (H5 fix)
from libs.shared.extraction.validators import FieldValidator
from libs.shared.extraction.cross_field_validator import CrossFieldValidator
from libs.shared.extraction.hitl import compute_field_flags
from libs.shared.clients.task_client import TaskClient
from libs.shared.clients.prior_auth_client import PriorAuthClient

import threading as _threading

logger = logging.getLogger(__name__)

# Module-level LayoutLM singleton — loaded once per worker process, reused across tasks.
# Avoids ~30-120s model reload on every job.
_LAYOUTLM_CLIENT_SINGLETON: Any = None
_LAYOUTLM_EXTRACTOR_SINGLETON: Any = None
_LAYOUTLM_LOCK = _threading.Lock()


def _get_layoutlm_extractor(settings: Any) -> Any:
    """Return the cached LayoutLMExtractor, loading model on first call."""
    global _LAYOUTLM_CLIENT_SINGLETON, _LAYOUTLM_EXTRACTOR_SINGLETON
    if _LAYOUTLM_EXTRACTOR_SINGLETON is not None:
        return _LAYOUTLM_EXTRACTOR_SINGLETON
    with _LAYOUTLM_LOCK:
        # Double-checked locking
        if _LAYOUTLM_EXTRACTOR_SINGLETON is not None:
            return _LAYOUTLM_EXTRACTOR_SINGLETON
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

    Orchestrates the pipeline by delegating to stage modules in
    ``workers.fax_processing_worker.tasks.stages``.

    Args:
        fax_job_id: UUID of the fax job to process.
        tenant_id: Tenant identifier.

    Returns:
        Dictionary with processing results.
    """
    from .stages import PipelineContext
    from .stages import ingestion, classification, extraction
    from .stages import post_processing, validation, finalization

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

        # Build pipeline context
        ctx = PipelineContext(
            fax_job_id=fax_job_id,
            job_uuid=job_uuid,
            tenant_id=tenant_id,
            db=db,
            settings=settings,
            job=job,
            pipeline_start=time.perf_counter(),
            metrics=PipelineMetrics(fax_job_id=fax_job_id, tenant_id=tenant_id),
        )
        ctx.init_repos()
        ctx.init_components()

        try:
            # ── Stage 1: Ingestion (steps 1–4b) ─────────────────────
            ingestion.load_and_split(ctx)
            ingestion.preprocess_and_ocr(ctx)
            ingestion.cover_page_redetection(ctx)
            ingestion.composite_document_detection(ctx)
            ingestion.generate_embeddings(ctx)

            # ── Stage 2: Classification (steps 5–7) ─────────────────
            classification.detect_payer(ctx)
            classification.match_template(ctx)
            classification.apply_page_rotation(ctx)
            classification.classify_document(ctx)

            # ── Stage 3: Extraction (steps 8–9c) ────────────────────
            extraction.template_extraction(ctx)
            extraction.ocr_label_extraction(ctx)
            extraction.layoutlm_extraction(ctx)
            extraction.add_decision_from_classifier(ctx)
            extraction.vlm_prescreening(ctx)

            # ── Stage 4: Post-processing (steps 10–10d) ─────────────
            post_processing.merge_fields(ctx)
            post_processing.smart_corrections(ctx)
            post_processing.ocr_scanners(ctx)

            # ── Stage 5: Validation (steps 11–15) ───────────────────
            validation.validate_fields(ctx)
            validation.cross_field_checks(ctx)
            validation.confidence_scoring(ctx)
            validation.determine_review(ctx)

            # ── Stage 6: Finalization (steps 16–17) ──────────────────
            finalization.store_extraction(ctx)
            finalization.hitl_flagging(ctx)
            finalization.store_job_metadata(ctx)
            finalization.create_review_or_finalize(ctx)
            finalization.finalize_metrics(ctx)

            return {
                "fax_job_id": fax_job_id,
                "status": ctx.job.status.value,
                "pages": len(ctx.pages),
                "payer": ctx.detected_payer.value,
                "doc_type": ctx.job.doc_type.value if ctx.job.doc_type else None,
                "template_matched": (
                    ctx.match_result.matched if ctx.match_result else False
                ),
                "template_fields": sum(
                    1 for c in ctx.candidates_by_field.values()
                    if any(
                        x.method == ExtractionMethodEnum.TEMPLATE_OCR for x in c
                    )
                ),
                "vlm_fields": (
                    ctx.vlm_extraction_meta.get("field_count", 0)
                    if ctx.vlm_extraction_meta
                    else 0
                ),
                "overall_confidence": ctx.overall_conf,
                "needs_review": ctx.needs_review,
                "fields_extracted": len(ctx.extracted_fields),
                "validation_failures": ctx.scoring_result.validation_failure_count,
                "cross_field_consistent": ctx.cross_result.is_consistent,
                "review_reasons": ctx.scoring_result.review_reasons,
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

        try:
            # Check for encryption / password protection
            if doc.is_encrypted:
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

            return pages
        finally:
            doc.close()

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
