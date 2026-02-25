"""
Fax upload and management endpoints.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from libs.shared.config import get_settings
from libs.shared.db.models.enums import FaxJobStatusEnum, PayerNameEnum
from libs.shared.db.models.fax_job import FaxJob
from libs.shared.db.repositories.fax_job_repo import FaxJobRepository
from libs.shared.db.session import get_db
from libs.shared.security.auth import AuthUser, get_current_user
from libs.shared.security.audit import get_audit_logger
from libs.shared.security.file_validation import sanitize_filename, validate_file_magic
from libs.shared.security.rate_limiter import get_upload_rate_limiter
from libs.shared.storage.s3_adapter import S3StorageAdapter

router = APIRouter()


# Pydantic schemas
class FaxUploadResponse(BaseModel):
    """Response for fax upload."""

    fax_job_id: UUID
    status: str
    created_at: datetime
    message: str = "Fax uploaded successfully and queued for processing"


class FaxJobResponse(BaseModel):
    """Response for fax job details."""

    fax_job_id: UUID
    tenant_id: str
    original_filename: str
    status: str
    total_pages: int | None
    payer_hint: str | None
    doc_type: str | None
    overall_conf: float | None
    needs_review: bool
    created_at: datetime
    processing_started_at: datetime | None
    processing_completed_at: datetime | None

    class Config:
        from_attributes = True


class FaxListResponse(BaseModel):
    """Response for listing faxes."""

    faxes: list[FaxJobResponse]
    total: int
    skip: int
    limit: int


class OcrTokenResponse(BaseModel):
    """OCR token with position and confidence."""

    token_text: str
    line_number: int
    word_number: int
    confidence: float
    bbox: dict[str, float]


class PageOcrResponse(BaseModel):
    """OCR data for a single page."""

    page_number: int
    width_px: int
    height_px: int
    full_text: str
    tokens: list[OcrTokenResponse]


class FaxOcrResponse(BaseModel):
    """Full OCR extraction response."""

    fax_job_id: UUID
    original_filename: str
    status: str
    total_pages: int | None
    pages: list[PageOcrResponse]


# Allowed file types
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/tiff",
    "image/tif",
    "image/png",
    "image/jpeg",
    "image/jpg",
}

# Maximum pages to prevent memory exhaustion
MAX_PAGES = 100


def get_storage() -> S3StorageAdapter:
    """Dependency for storage adapter."""
    return S3StorageAdapter()


def _build_duplicate_response(existing: FaxJob) -> JSONResponse:
    """Return a stable duplicate upload response payload."""
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "fax_job_id": str(existing.fax_job_id),
            "status": existing.status.value,
            "created_at": existing.created_at.isoformat(),
            "message": "Duplicate fax detected. Returning existing job.",
        },
    )


def _read_upload_with_limit(
    upload: UploadFile,
    max_size_bytes: int,
    chunk_size: int = 1024 * 1024,
) -> bytes:
    """Read uploaded file in bounded chunks to avoid unbounded RAM usage."""
    chunks: list[bytes] = []
    total = 0

    while True:
        chunk = upload.file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File too large. Maximum size: {max_size_bytes // (1024 * 1024)}MB",
            )
        chunks.append(chunk)

    return b"".join(chunks)


def _maybe_requeue_existing(existing: FaxJob) -> None:
    """Re-queue existing job if it is currently pending or previously failed.

    Uses a deterministic task_id derived from the fax_job_id to prevent
    duplicate tasks when concurrent uploads hit the same duplicate.
    """
    stale_processing = (
        existing.status == FaxJobStatusEnum.PROCESSING
        and existing.processing_started_at is not None
        and (datetime.now(timezone.utc) - existing.processing_started_at) > timedelta(minutes=20)
    )
    if existing.status not in (FaxJobStatusEnum.PENDING, FaxJobStatusEnum.FAILED) and not stale_processing:
        return

    if stale_processing:
        logger.warning(
            "Detected stale PROCESSING job %s (started_at=%s); forcing requeue",
            existing.fax_job_id,
            existing.processing_started_at,
        )
        existing.status = FaxJobStatusEnum.FAILED

    try:
        logger.info(
            "Re-queuing task for existing %s job %s",
            existing.status.value,
            existing.fax_job_id,
        )
        from workers.fax_processing_worker.celery_app import app as celery_app
        # Deterministic task_id prevents duplicate tasks from concurrent re-queues
        task_id = f"requeue-{existing.fax_job_id}"
        result = celery_app.send_task(
            "workers.fax_processing_worker.tasks.process_fax.process_fax_task",
            args=[str(existing.fax_job_id), existing.tenant_id],
            queue="fax_processing",
            task_id=task_id,
        )
        logger.info("Task re-queued for job %s, task_id=%s", existing.fax_job_id, result.id)
    except Exception as e:
        logger.error("Failed to re-queue task for job %s: %s", existing.fax_job_id, e)


@router.post("/upload", response_model=FaxUploadResponse, status_code=status.HTTP_201_CREATED)
def upload_fax(
    request: Request,
    file: Annotated[UploadFile, File(description="Fax file (PDF, TIFF, or image)")],
    tenant_id: Annotated[str, Form(description="Tenant identifier")],
    payer_hint: Annotated[str | None, Form(description="Optional payer hint")] = None,
    external_fax_id: Annotated[str | None, Form(description="Optional external reference")] = None,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_storage),
) -> FaxUploadResponse:
    """
    Upload a fax for processing.

    Accepts PDF, TIFF, and image files. The fax will be stored in
    MinIO and queued for OCR processing.
    """
    settings = get_settings()

    # Rate limiting
    rate_limiter = get_upload_rate_limiter()
    rate_limiter.check(request)

    # Tenant isolation: authenticated user's tenant must match
    if settings.environment != "development" and user.tenant_id != tenant_id:
        if not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot upload to a different tenant",
            )

    # Validate content type
    content_type = file.content_type or ""
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Allowed types: PDF, TIFF, PNG, JPEG",
        )

    # Validate file size
    max_size = settings.api.max_upload_size_mb * 1024 * 1024
    content_length_header = request.headers.get("content-length")
    if content_length_header:
        try:
            content_length = int(content_length_header)
        except ValueError:
            content_length = 0
        # Multipart adds overhead; allow a small envelope but reject obvious abuse.
        if content_length > (max_size + 2 * 1024 * 1024):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File too large. Maximum size: {settings.api.max_upload_size_mb}MB",
            )

    # Read file content (sync — FastAPI runs sync endpoints in threadpool)
    content = _read_upload_with_limit(file, max_size)

    # Validate file magic bytes (prevents extension spoofing)
    validate_file_magic(content, content_type)

    # Sanitize filename (prevents path traversal, null bytes, etc.)
    filename = sanitize_filename(file.filename)

    # Compute SHA-256 hash
    file_hash = hashlib.sha256(content).hexdigest()

    # Check for duplicate
    repo = FaxJobRepository(db)
    existing = repo.get_by_sha256(file_hash, tenant_id=tenant_id)
    if existing:
        _maybe_requeue_existing(existing)
        return _build_duplicate_response(existing)

    # Generate storage key
    timestamp = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    storage_key = f"{tenant_id}/{timestamp}/{file_hash[:8]}_{filename}"

    # Upload to storage
    try:
        storage.upload(
            key=storage_key,
            data=content,
            content_type=content_type,
            metadata={
                "tenant_id": tenant_id,
                "original_filename": filename,
            },
        )
    except Exception as e:
        logger.error("File storage failed (sha256=%s...): %s", file_hash[:8], e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store file. Please try again.",
        )

    # Parse payer hint
    payer_enum = PayerNameEnum.UNKNOWN
    if payer_hint:
        try:
            payer_enum = PayerNameEnum(payer_hint.upper())
        except ValueError:
            pass

    # Create fax job
    fax_job = FaxJob(
        tenant_id=tenant_id,
        original_filename=filename,
        file_storage_key=storage_key,
        file_sha256=file_hash,
        file_size_bytes=len(content),
        payer_hint=payer_enum,
        external_fax_id=external_fax_id,
        status=FaxJobStatusEnum.PENDING,
    )

    repo.create(fax_job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = repo.get_by_sha256(file_hash, tenant_id=tenant_id)
        if existing:
            logger.info(
                "Duplicate detected at commit-time for sha256=%s...; returning existing job %s",
                file_hash[:8],
                existing.fax_job_id,
            )
            _maybe_requeue_existing(existing)
            existing_storage_key = getattr(existing, "file_storage_key", None)
            if existing_storage_key and existing_storage_key == storage_key:
                logger.info(
                    "Skipping duplicate object cleanup for sha256=%s... because key %s is used by existing job",
                    file_hash[:8],
                    storage_key,
                )
            else:
                try:
                    storage.delete(storage_key)
                except Exception:
                    logger.warning(
                        "Failed to delete duplicate object for job hash %s at key %s",
                        file_hash[:8],
                        storage_key,
                    )
            return _build_duplicate_response(existing)
        try:
            storage.delete(storage_key)
        except Exception:
            logger.warning(
                "Failed to clean up uploaded object after IntegrityError for hash %s at key %s",
                file_hash[:8],
                storage_key,
            )
        logger.exception("IntegrityError persisting fax job sha256=%s...", file_hash[:8])
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist fax job. Please retry.",
        )

    # Audit log
    audit = get_audit_logger(request, db)
    audit.log_create("fax_job", fax_job.fax_job_id, {"filename": filename})
    db.commit()

    # Queue processing task using the worker's Celery app
    try:
        from workers.fax_processing_worker.celery_app import app as celery_app
        result = celery_app.send_task(
            "workers.fax_processing_worker.tasks.process_fax.process_fax_task",
            args=[str(fax_job.fax_job_id), tenant_id],
            queue="fax_processing",
        )
        logger.info("Processing task queued for job %s, task_id=%s", fax_job.fax_job_id, result.id)
    except Exception:
        # Mark as FAILED so it doesn't stay stuck in PENDING indefinitely.
        # The client can retry by re-uploading the same file (duplicate detection re-queues it).
        logger.exception("Failed to queue processing task for job %s", fax_job.fax_job_id)
        try:
            repo.update_status(fax_job.fax_job_id, FaxJobStatusEnum.FAILED)
            db.commit()
        except Exception:
            logger.exception("Failed to update job status to FAILED for %s", fax_job.fax_job_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="File uploaded but processing queue is unavailable. Please retry.",
        )

    return FaxUploadResponse(
        fax_job_id=fax_job.fax_job_id,
        status=fax_job.status.value,
        created_at=fax_job.created_at,
    )


@router.get("/{fax_job_id}", response_model=FaxJobResponse)
def get_fax_job(
    request: Request,
    fax_job_id: UUID,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FaxJobResponse:
    """Get fax job details."""
    repo = FaxJobRepository(db)
    job = repo.get_by_id(fax_job_id)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Fax job not found: {fax_job_id}",
        )

    # Tenant isolation
    settings = get_settings()
    if settings.environment != "development" and job.tenant_id != user.tenant_id:
        if not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot access a job belonging to another tenant",
            )

    # Audit log
    audit = get_audit_logger(request, db)
    audit.log_read("fax_job", fax_job_id)
    db.commit()

    return FaxJobResponse(
        fax_job_id=job.fax_job_id,
        tenant_id=job.tenant_id,
        original_filename=job.original_filename,
        status=job.status.value,
        total_pages=job.total_pages,
        payer_hint=job.payer_hint.value if job.payer_hint else None,
        doc_type=job.doc_type.value if job.doc_type else None,
        overall_conf=float(job.overall_conf) if job.overall_conf else None,
        needs_review=job.needs_review,
        created_at=job.created_at,
        processing_started_at=job.processing_started_at,
        processing_completed_at=job.processing_completed_at,
    )


@router.get("/{fax_job_id}/results")
def get_fax_results(
    request: Request,
    fax_job_id: UUID,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return the clean client-facing extraction results for a completed fax job.

    This endpoint returns a simplified structure — one entry per field with
    just the final decided value, confidence score, source method, and a
    not_present flag when the field was not found in the document.

    For full internal detail (all candidates, evidence bounding boxes, etc.)
    use the review-packet endpoint on the Review API.
    """
    from libs.shared.db.repositories.extraction_repo import ExtractionRepository
    from libs.shared.extraction.output_formatter import format_summary

    repo = FaxJobRepository(db)
    job = repo.get_by_id(fax_job_id)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Fax job not found: {fax_job_id}",
        )

    settings = get_settings()
    if settings.environment != "development" and job.tenant_id != user.tenant_id:
        if not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot access results belonging to another tenant",
            )

    if job.status not in (
        FaxJobStatusEnum.COMPLETED,
        FaxJobStatusEnum.NEEDS_REVIEW,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Results not available — job status is {job.status.value}",
        )

    extraction_repo = ExtractionRepository(db)
    extraction = extraction_repo.get_by_job(fax_job_id)

    if not extraction or not extraction.extraction_json:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No extraction results found for this job",
        )

    audit = get_audit_logger(request, db)
    audit.log_read("fax_results", fax_job_id)
    db.commit()

    flagged_fields = getattr(extraction, "flagged_fields", None) or []

    return {
        "fax_job_id": str(fax_job_id),
        "payer": job.payer_hint.value if job.payer_hint else None,
        "doc_type": job.doc_type.value if job.doc_type else None,
        "status": job.status.value,
        "overall_confidence": float(job.overall_conf) if job.overall_conf else None,
        "needs_review": job.needs_review,
        **format_summary(extraction.extraction_json, flagged_fields),
    }


@router.get("/{fax_job_id}/ocr", response_model=FaxOcrResponse)
def get_fax_ocr(
    request: Request,
    fax_job_id: UUID,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FaxOcrResponse:
    """Get full OCR extraction data for a fax job."""
    repo = FaxJobRepository(db)
    job = repo.get_by_id_with_ocr(fax_job_id)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Fax job not found: {fax_job_id}",
        )

    # Tenant isolation
    settings = get_settings()
    if settings.environment != "development" and job.tenant_id != user.tenant_id:
        if not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot access OCR data belonging to another tenant",
            )

    # Audit log (accessing PHI)
    audit = get_audit_logger(request, db)
    audit.log_read("fax_ocr", fax_job_id)
    db.commit()

    # Build page OCR responses
    pages_ocr = []
    if job.pages:
        for page in sorted(job.pages, key=lambda p: p.page_number):
            tokens = []
            for token in page.ocr_tokens:
                tokens.append(OcrTokenResponse(
                    token_text=token.token_text,
                    line_number=token.line_number,
                    word_number=token.word_number,
                    confidence=float(token.confidence),
                    bbox={
                        "x0": float(token.bbox_x0),
                        "y0": float(token.bbox_y0),
                        "x1": float(token.bbox_x1),
                        "y1": float(token.bbox_y1),
                    },
                ))

            pages_ocr.append(PageOcrResponse(
                page_number=page.page_number,
                width_px=page.width_px,
                height_px=page.height_px,
                full_text=page.get_full_text(),
                tokens=tokens,
            ))

    return FaxOcrResponse(
        fax_job_id=job.fax_job_id,
        original_filename=job.original_filename,
        status=job.status.value,
        total_pages=job.total_pages,
        pages=pages_ocr,
    )


@router.get("", response_model=FaxListResponse)
def list_faxes(
    request: Request,
    tenant_id: str | None = None,
    status_filter: str | None = None,
    skip: int = 0,
    limit: int = 100,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FaxListResponse:
    """List fax jobs. Admins may omit tenant_id to list all tenants."""
    # Cap limit to prevent accidental bulk data dumps
    limit = min(limit, 500)

    # Tenant isolation — non-admins always see only their own tenant
    if not user.is_admin:
        tenant_id = user.tenant_id

    repo = FaxJobRepository(db)

    # Parse status filter
    status_enum = None
    if status_filter:
        try:
            status_enum = FaxJobStatusEnum(status_filter.upper())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status filter: {status_filter}",
            )

    jobs = repo.get_by_tenant(tenant_id, status_enum, skip, limit)
    total = repo.count_by_tenant(tenant_id, status_enum)

    # Audit log (HIPAA: listing PHI records)
    audit = get_audit_logger(request, db)
    audit.log_read("fax_job_list", None, {"tenant_id": tenant_id, "count": len(jobs)})
    db.commit()

    return FaxListResponse(
        faxes=[
            FaxJobResponse(
                fax_job_id=job.fax_job_id,
                tenant_id=job.tenant_id,
                original_filename=job.original_filename,
                status=job.status.value,
                total_pages=job.total_pages,
                payer_hint=job.payer_hint.value if job.payer_hint else None,
                doc_type=job.doc_type.value if job.doc_type else None,
                overall_conf=float(job.overall_conf) if job.overall_conf else None,
                needs_review=job.needs_review,
                created_at=job.created_at,
                processing_started_at=job.processing_started_at,
                processing_completed_at=job.processing_completed_at,
            )
            for job in jobs
        ],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.delete("/{fax_job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_fax_job(
    request: Request,
    fax_job_id: UUID,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_storage),
) -> None:
    """Delete a fax job and its associated files."""
    repo = FaxJobRepository(db)
    job = repo.get_by_id(fax_job_id)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Fax job not found: {fax_job_id}",
        )

    # Tenant isolation: non-admin users can only delete their own tenant's jobs
    settings = get_settings()
    if settings.environment != "development" and job.tenant_id != user.tenant_id:
        if not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot delete a job belonging to another tenant",
            )

    # Audit log (before deletion)
    audit = get_audit_logger(request, db)
    audit.log_delete("fax_job", fax_job_id, {"filename": job.original_filename})

    # Delete from storage
    try:
        storage.delete(job.file_storage_key)
    except Exception:
        pass  # Storage deletion is best-effort

    # Delete derived page images (e.g., "{file_storage_key}_page_1.png")
    derived_prefix = f"{job.file_storage_key}_page_"
    try:
        for derived_key in storage.list_keys(prefix=derived_prefix, limit=5000):
            try:
                storage.delete(derived_key)
            except Exception:
                logger.warning(
                    "Best-effort delete failed for derived object %s",
                    derived_key,
                    exc_info=True,
                )
    except Exception:
        logger.warning(
            "Failed to list derived storage objects for prefix %s",
            derived_prefix,
            exc_info=True,
        )

    # Delete from database (cascades to pages, tokens, etc.)
    repo.delete(job)
    db.commit()
