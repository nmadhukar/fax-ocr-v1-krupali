"""
Stage 1: Ingestion — Load, split, preprocess, OCR, embeddings.

Steps 1–4b of the fax processing pipeline.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

import cv2
import numpy as np
from sqlalchemy import text as sa_text

from libs.shared.db.models.enums import ExtractionMethodEnum
from libs.shared.db.models.fax_page import FaxPage

from . import PipelineContext

logger = logging.getLogger(__name__)

# ── Cover-page OCR re-detection signals ──────────────────────────────
_COVER_SIGNALS = [
    (r"fax\s+cover\s+sheet", 2.0),
    (r"\bcover\s+sheet\b", 1.5),
    (r"facsimile\s+transmission", 1.5),
    (r"confidentiality\s+notice", 1.0),
    (r"(?m)^to\s*:", 0.5),
    (r"(?m)^from\s*:", 0.5),
    (r"(?m)^subject\s*:", 0.5),
    (r"(?m)^pages?\s*:", 0.5),
]

_AUTH_CONTENT_KEYWORDS = [
    "authorization", "health plan id", "member name", "member id",
    "prior auth", "reference#", "cpt code", "hcpcs",
    "procedure code", "service code", "diagnosis",
    "auth status", "approved", "auth #",
]


def load_and_split(ctx: PipelineContext) -> None:
    """Step 1-2: Download file from storage and split into page images."""
    file_bytes = ctx.storage.download(ctx.job.file_storage_key)
    if not file_bytes:
        raise ValueError(
            f"Downloaded file is empty for job {ctx.fax_job_id}: {ctx.job.original_filename}"
        )

    # Import page splitter from the parent module
    from workers.fax_processing_worker.tasks.process_fax import split_to_pages

    try:
        ctx.pages = split_to_pages(file_bytes, ctx.job.original_filename)
    except Exception as exc:
        raise ValueError(
            f"Failed to parse fax document '{ctx.job.original_filename}': {exc}"
        ) from exc

    if not ctx.pages:
        raise ValueError(
            f"Document contains no pages: {ctx.job.original_filename}"
        )
    ctx.job.total_pages = len(ctx.pages)


def preprocess_and_ocr(ctx: PipelineContext) -> None:
    """Steps 3-4: Preprocess each page, run OCR, store tokens."""
    # Clean up partial data from previous attempts (retry-safe)
    for stmt in [
        "DELETE FROM fax_review WHERE fax_job_id = :jid",
        "DELETE FROM fax_extracted_field WHERE fax_job_id = :jid",
        "DELETE FROM fax_ocr_token WHERE fax_page_id IN "
        "(SELECT fax_page_id FROM fax_page WHERE fax_job_id = :jid)",
        "DELETE FROM fax_page WHERE fax_job_id = :jid",
    ]:
        ctx.db.execute(sa_text(stmt), {"jid": str(ctx.job_uuid)})
    ctx.db.flush()

    pages_with_tokens = 0
    for page_num, page_image in enumerate(ctx.pages, start=1):
        if page_image is None or not hasattr(page_image, "shape"):
            logger.warning(
                "Skipping invalid page image at page %d for job %s",
                page_num, ctx.fax_job_id,
            )
            continue
        height, width = page_image.shape[:2]

        # Store original page image
        page_storage_key = f"{ctx.job.file_storage_key}_page_{page_num}.png"
        ok, page_png = cv2.imencode(".png", page_image)
        if not ok or page_png is None:
            raise RuntimeError(
                f"Failed to encode page {page_num} as PNG for job {ctx.fax_job_id}"
            )
        ctx.storage.upload(
            key=page_storage_key,
            data=page_png.tobytes(),
            content_type="image/png",
        )

        # Preprocess
        processed, page_quality = ctx.preprocessor.preprocess(page_image)
        if processed is None:
            logger.warning(
                "Preprocessing returned None for page %d of job %s; using original image",
                page_num,
                ctx.fax_job_id,
            )
            processed = page_image

        # Create page record
        fax_page = FaxPage(
            fax_job_id=ctx.job_uuid,
            page_number=page_num,
            page_storage_key=page_storage_key,
            width_px=width,
            height_px=height,
            dpi=300,
            blur_score=page_quality.blur_score if page_quality else None,
            skew_angle_deg=page_quality.skew_angle_deg if page_quality else None,
            text_density=page_quality.text_density if page_quality else None,
            is_cover_page=ctx.preprocessor.detect_cover_page(page_image),
        )
        ctx.page_repo.create(fax_page)
        ctx.page_id_map[page_num] = fax_page.fax_page_id

        # OCR extraction
        try:
            ocr_result = ctx.ocr_client.extract(processed)
        except Exception:
            logger.warning(
                "OCR failed for page %d of job %s",
                page_num,
                ctx.fax_job_id,
                exc_info=True,
            )
            continue

        tokens_data = []
        if ocr_result is None or not hasattr(ocr_result, "tokens"):
            logger.warning(
                "OCR returned None for page %d of job %s",
                page_num, ctx.fax_job_id,
            )
            continue

        for token in ocr_result.tokens:
            token_dict = token.to_dict()
            token_dict["fax_page_id"] = fax_page.fax_page_id
            tokens_data.append(token_dict)

        if tokens_data:
            ctx.token_repo.bulk_insert(tokens_data)
            pages_with_tokens += 1
        else:
            logger.warning(
                "OCR produced zero tokens for page %d of job %s",
                page_num,
                ctx.fax_job_id,
            )

    if pages_with_tokens == 0:
        raise RuntimeError(
            f"OCR produced zero tokens for all pages in job {ctx.fax_job_id}"
        )

    ctx.db.commit()

    # Gather OCR text from all pages
    _rebuild_ocr_text(ctx)


def cover_page_redetection(ctx: PipelineContext) -> None:
    """Step 3b: OCR-based cover page re-detection."""
    for page_num in range(1, len(ctx.pages) + 1):
        pr = ctx.page_repo.get_page_with_tokens(ctx.job_uuid, page_num)
        if not pr or pr.is_cover_page:
            continue
        pt = pr.get_full_text()
        cover_score = sum(
            w for pat, w in _COVER_SIGNALS
            if re.search(pat, pt, re.IGNORECASE)
        )
        has_auth = any(kw in pt.lower() for kw in _AUTH_CONTENT_KEYWORDS)
        if cover_score >= 1.5 and not has_auth:
            try:
                ctx.page_repo.mark_as_cover_page(pr.fax_page_id)
                logger.info(
                    "OCR cover re-detection: page %d marked as cover "
                    "(score=%.1f, no auth content)",
                    page_num, cover_score,
                )
            except Exception:
                logger.warning(
                    "OCR cover re-detection: failed to mark page %d",
                    page_num, exc_info=True,
                )

    # Rebuild OCR text after cover re-detection
    _rebuild_ocr_text(ctx)
    ctx.db.commit()


def composite_document_detection(ctx: PipelineContext) -> None:
    """Step 4a: Detect composite (multi-document) faxes."""
    if len(ctx.pages) < 4:
        return

    from libs.shared.classification.document_splitter import DocumentSplitter

    splitter = DocumentSplitter()
    page_texts_for_split: dict[int, str] = {}
    cover_page_set: set[int] = set()

    for page_num in range(1, len(ctx.pages) + 1):
        pr = ctx.page_repo.get_page_with_tokens(ctx.job_uuid, page_num)
        if pr:
            page_texts_for_split[page_num] = pr.get_full_text()
            if pr.is_cover_page:
                cover_page_set.add(page_num)

    split_result = splitter.detect_segments(page_texts_for_split, cover_page_set)
    ctx.split_meta = split_result.to_dict()

    if split_result.is_composite and split_result.segments:
        best_seg = max(split_result.segments, key=lambda s: s.payer_confidence)
        ctx.active_page_numbers = set(best_seg.content_pages)

        logger.info(
            "Composite fax: %d segments detected, focusing on segment %d (pages %s, payer=%s)",
            len(split_result.segments),
            best_seg.segment_index,
            best_seg.page_numbers,
            best_seg.detected_payer,
        )
        # Keep downstream extraction/scanners strictly scoped to the selected segment.
        _rebuild_ocr_text(ctx)
    elif split_result.is_composite:
        logger.warning(
            "Composite detection returned no segments for job %s; using full document",
            ctx.fax_job_id,
        )


def generate_embeddings(ctx: PipelineContext) -> None:
    """Step 4b: Generate embeddings (sentence-transformers)."""
    if not ctx.settings.features.vector_search or not ctx.all_ocr_text:
        return

    try:
        from libs.shared.embeddings import get_embedding_client
        from libs.shared.db.repositories.embedding_repo import EmbeddingRepository

        emb_client = get_embedding_client()
        if not emb_client.is_available():
            logger.info("Embedding model not available; skipping")
            return

        emb_repo = EmbeddingRepository(ctx.db)
        clean_text = emb_client.clean_ocr_text(ctx.all_ocr_text)
        chunks = emb_client.chunk_text(clean_text)
        if chunks:
            vectors = emb_client.encode(chunks)
            pair_count = min(len(chunks), len(vectors))
            if pair_count == 0:
                logger.warning(
                    "Embedding model returned zero vectors for %d chunks [job=%s]",
                    len(chunks),
                    ctx.fax_job_id,
                )
                return
            if pair_count != len(chunks):
                logger.warning(
                    "Embedding vector count mismatch: %d chunks vs %d vectors [job=%s]",
                    len(chunks),
                    len(vectors),
                    ctx.fax_job_id,
                )
            emb_rows = [
                {
                    "fax_job_id": ctx.job_uuid,
                    "fax_page_id": None,
                    "embedding": vectors[idx],
                    "source_text": chunks[idx],
                    "source_type": "page_text",
                    "chunk_index": idx,
                    "token_count": len(chunks[idx].split()),
                }
                for idx in range(pair_count)
            ]
            try:
                # Isolate optional embedding writes from core OCR transaction.
                # If pgvector insert fails, keep processing the fax.
                with ctx.db.begin_nested():
                    count = emb_repo.bulk_insert_embeddings(emb_rows)
                    ctx.db.flush()
                logger.info("Generated %d embeddings (%d chunks)", count, len(chunks))
            except Exception:
                logger.warning("Embedding generation failed; skipping", exc_info=True)

    except Exception:
        logger.warning("Embedding generation failed; skipping", exc_info=True)


# ── Helpers ──────────────────────────────────────────────────────────

def _rebuild_ocr_text(ctx: PipelineContext) -> None:
    """Rebuild all_page_text / all_ocr_text from DB page records."""
    ctx.all_page_text = ""
    ctx.all_ocr_text = ""
    ctx.first_content_page = None
    ctx.first_content_page_text = ""

    if ctx.active_page_numbers:
        page_numbers = [
            p for p in sorted(ctx.active_page_numbers)
            if 1 <= p <= len(ctx.pages)
        ]
    else:
        page_numbers = list(range(1, len(ctx.pages) + 1))

    for page_num in page_numbers:
        pr = ctx.page_repo.get_page_with_tokens(ctx.job_uuid, page_num)
        if pr:
            pt = pr.get_full_text()
            ctx.all_page_text += pt + "\n\n"
            if not pr.is_cover_page:
                ctx.all_ocr_text += pt + "\n\n"
                if ctx.first_content_page is None:
                    ctx.first_content_page = pr
                    ctx.first_content_page_text = pt

    ctx.all_page_text = ctx.all_page_text.strip()
    ctx.all_ocr_text = ctx.all_ocr_text.strip()
    ctx.full_doc_ocr_text = ctx.all_ocr_text
