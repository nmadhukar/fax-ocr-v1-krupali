"""
Review workflow endpoints.
"""

import logging
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from libs.shared.config import get_settings
from libs.shared.db.models.fax_extraction import FaxExtraction
from libs.shared.db.models.fax_review import FaxFeedback
from libs.shared.db.repositories.extraction_repo import ExtractionRepository
from libs.shared.db.repositories.fax_job_repo import FaxJobRepository
from libs.shared.db.repositories.fax_page_repo import FaxPageRepository
from libs.shared.db.repositories.review_repo import FeedbackRepository, ReviewRepository
from libs.shared.db.repositories.template_repo import TemplateFieldRepository
from libs.shared.db.session import get_db
from libs.shared.security.auth import AuthUser, get_current_user
from libs.shared.security.audit import get_audit_logger
from libs.shared.storage.s3_adapter import S3StorageAdapter

logger = logging.getLogger(__name__)

router = APIRouter()
_FIELD_KEY_RE = re.compile(r"^[a-z0-9_]+$")


# Pydantic schemas
class PageInfo(BaseModel):
    """Page information for review packet."""

    page_number: int
    page_id: str
    image_url: str
    width_px: int
    height_px: int


class FieldCandidate(BaseModel):
    """Extraction candidate."""

    value: str
    method: str
    confidence: float
    evidence_bbox: dict[str, Any] | None = None


class ExtractedFieldInfo(BaseModel):
    """Extracted field for review."""

    field_key: str
    value: str | None
    confidence: float | None
    evidence_bbox: dict[str, Any] | None
    candidates: list[FieldCandidate]
    # HITL flags — surfaced so the UI can highlight low-confidence fields
    is_flagged: bool = False
    flag_reason: str | None = None  # LOW_CONFIDENCE | MISSING_VALUE


class ReviewPacketResponse(BaseModel):
    """Complete review packet response."""

    fax_job_id: str
    pages: list[PageInfo]
    extracted_fields: list[ExtractedFieldInfo]
    template_match: dict[str, Any] | None
    review_reasons: list[str]
    created_at: datetime
    # HITL — ordered list of flagged fields (critical first) for the reviewer UI
    flagged_fields: list[dict[str, Any]] = []


class ClaimRequest(BaseModel):
    """Request to claim a review."""

    reviewer_id: str | None = None


class ClaimResponse(BaseModel):
    """Response for claim request."""

    review_id: str
    claimed_by: str
    claimed_at: datetime
    expires_at: datetime


class FieldCorrection(BaseModel):
    """Corrected field value."""

    field_key: str = Field(..., min_length=1, max_length=100)
    corrected_value: str = Field(..., min_length=1, max_length=2048)
    evidence_bbox: dict[str, float] | None = None


class SubmitRequest(BaseModel):
    """Request to submit a review."""

    reviewer_id: str | None = None
    corrected_fields: list[FieldCorrection] = Field(default_factory=list, max_length=200)


class SubmitResponse(BaseModel):
    """Response for submit request."""

    review_id: str
    submitted_by: str
    submitted_at: datetime
    corrections_count: int


class ReviewListItem(BaseModel):
    """Review list item."""

    review_id: str
    fax_job_id: str
    priority: int
    review_reasons: list[str]
    claimed_by: str | None
    created_at: datetime


def get_storage() -> S3StorageAdapter:
    """Dependency for storage adapter."""
    return S3StorageAdapter()


def _collect_allowed_correction_fields(
    db: Session,
    job: Any,
    extraction: FaxExtraction | None,
    review_packet: dict[str, Any] | None,
) -> set[str]:
    """
    Build the allowlist of field keys that reviewers may correct.

    Sources:
      1) Existing extraction_json keys.
      2) review_packet.extracted_fields keys.
      3) Matched template field definitions (if available).
      4) Known OCR-label fallback field keys.
      5) HITL threshold table keys.
    """
    allowed: set[str] = set()

    if extraction and isinstance(extraction.extraction_json, dict):
        for key in extraction.extraction_json:
            if isinstance(key, str):
                k = key.strip().lower()
                if k:
                    allowed.add(k)

    if isinstance(review_packet, dict):
        packet_fields = review_packet.get("extracted_fields")
        if isinstance(packet_fields, dict):
            for key in packet_fields:
                if isinstance(key, str):
                    k = key.strip().lower()
                    if k:
                        allowed.add(k)
        elif isinstance(packet_fields, list):
            for item in packet_fields:
                if isinstance(item, dict):
                    key = item.get("field_key")
                    if isinstance(key, str):
                        k = key.strip().lower()
                        if k:
                            allowed.add(k)

    template_version_id = getattr(job, "matched_template_version_id", None)
    if template_version_id:
        try:
            template_fields = TemplateFieldRepository(db).get_by_version(template_version_id)
            for field in template_fields:
                if isinstance(field.field_key, str):
                    k = field.field_key.strip().lower()
                    if k:
                        allowed.add(k)
        except Exception:
            logger.warning(
                "Failed to load template fields for correction allowlist [version=%s]",
                str(template_version_id)[:8],
                exc_info=True,
            )

    try:
        from libs.shared.extraction.ocr_label_extractor import LABEL_ALIASES

        for key in LABEL_ALIASES.keys():
            k = key.strip().lower()
            if k:
                allowed.add(k)
    except Exception:
        logger.warning("Failed to load OCR-label field aliases for correction allowlist", exc_info=True)

    try:
        from libs.shared.extraction.hitl import FIELD_REVIEW_THRESHOLDS

        for key in FIELD_REVIEW_THRESHOLDS.keys():
            k = key.strip().lower()
            if k:
                allowed.add(k)
    except Exception:
        logger.warning("Failed to load HITL field thresholds for correction allowlist", exc_info=True)

    return allowed


def _normalize_and_validate_corrections(
    corrected_fields: list[FieldCorrection],
    allowed_field_keys: set[str],
) -> list[dict[str, Any]]:
    """
    Normalize reviewer corrections and validate keys.

    - field_key is normalized to lowercase snake_case.
    - duplicate field_key entries are rejected.
    - keys outside the allowlist are rejected.
    """
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates: set[str] = set()
    invalid: set[str] = set()

    for correction in corrected_fields:
        field_key = (correction.field_key or "").strip().lower()
        if not field_key or not _FIELD_KEY_RE.fullmatch(field_key):
            invalid.add(correction.field_key or "")
            continue

        if field_key in seen:
            duplicates.add(field_key)
            continue

        if field_key not in allowed_field_keys:
            invalid.add(correction.field_key or field_key)
            continue

        seen.add(field_key)
        normalized.append({
            "field_key": field_key,
            "corrected_value": correction.corrected_value,
            "evidence_bbox": correction.evidence_bbox,
        })

    if duplicates:
        duplicate_list = ", ".join(sorted(duplicates))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Duplicate corrected field_key values are not allowed: {duplicate_list}",
        )

    if invalid:
        invalid_list = ", ".join(sorted(k for k in invalid if k))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid or unsupported field_key values: {invalid_list}",
        )

    return normalized


def _extract_page_number_from_bbox(bbox: dict[str, Any] | None) -> int | None:
    """Extract page number from bbox metadata if present."""
    if not isinstance(bbox, dict):
        return None
    for key in ("page", "page_number", "page_num"):
        raw = bbox.get(key)
        if raw is None:
            continue
        try:
            page_num = int(raw)
        except (TypeError, ValueError):
            continue
        if page_num >= 1:
            return page_num
    return None


@router.get("/{fax_job_id}/review-packet", response_model=ReviewPacketResponse)
def get_review_packet(
    http_request: Request,
    fax_job_id: UUID,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_storage),
) -> ReviewPacketResponse:
    """Get review packet for a fax job."""
    # Get job
    job_repo = FaxJobRepository(db)
    job = job_repo.get_by_id(fax_job_id)

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
                detail="Cannot access a review belonging to another tenant",
            )

    # Get review record
    review_repo = ReviewRepository(db)
    review = review_repo.get_by_job(fax_job_id)

    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No review found for job: {fax_job_id}",
        )

    # Audit log (PHI access) — flush immediately so the audit record persists
    # even if a subsequent read operation fails (HIPAA compliance).
    audit = get_audit_logger(http_request, db)
    audit.log_read("fax_review", review.review_id, {"fax_job_id": str(fax_job_id)})
    db.flush()

    # Get pages
    page_repo = FaxPageRepository(db)
    pages = page_repo.get_by_job(fax_job_id)

    # Restrict reviewer page payload to the pages selected for this review packet
    # (important for composite faxes to avoid leaking unrelated pages).
    selected_page_numbers: set[int] | None = None
    if review.review_packet and isinstance(review.review_packet, dict):
        packet_pages = review.review_packet.get("pages")
        if isinstance(packet_pages, list):
            selected_page_numbers = set()
            for item in packet_pages:
                if not isinstance(item, dict):
                    continue
                try:
                    page_num = int(item.get("page_number"))
                except (TypeError, ValueError):
                    continue
                if page_num >= 1:
                    selected_page_numbers.add(page_num)

    if selected_page_numbers is not None:
        pages = [p for p in pages if p.page_number in selected_page_numbers]

    page_info = []
    for page in pages:
        # Generate presigned URL
        image_url = storage.get_url(page.page_storage_key, expires_in=3600)

        # Audit: downloading page image (PHI)
        audit.log_download("fax_page", page.fax_page_id)

        page_info.append(PageInfo(
            page_number=page.page_number,
            page_id=str(page.fax_page_id),
            image_url=image_url,
            width_px=page.width_px,
            height_px=page.height_px,
        ))

    # Get extraction
    extraction_repo = ExtractionRepository(db)
    extraction = extraction_repo.get_by_job(fax_job_id)

    # Build a fast lookup: field_key → flag reason for the HITL layer
    flagged_map: dict[str, str] = {}
    stored_flags: list[dict] = []
    if extraction and hasattr(extraction, "flagged_fields") and extraction.flagged_fields:
        for flag in extraction.flagged_fields:
            fk = flag.get("field_key")
            # Support both "reason" (HITL internal) and "flag_reason" (API key)
            reason = flag.get("reason") or flag.get("flag_reason", "LOW_CONFIDENCE")
            if fk:
                flagged_map[fk] = reason
            # Normalize to consistent API shape with flag_reason key
            stored_flags.append({
                "field_key": fk,
                "flag_reason": reason,
                "confidence": flag.get("confidence"),
                "threshold": flag.get("threshold"),
            })

    extracted_fields = []
    if extraction and extraction.extraction_json:
        for field_key, field_data in extraction.extraction_json.items():
            if isinstance(field_data, dict):
                candidates = field_data.get("candidates", [])
                extracted_fields.append(ExtractedFieldInfo(
                    field_key=field_key,
                    value=field_data.get("value"),
                    confidence=field_data.get("confidence"),
                    evidence_bbox=field_data.get("evidence_bbox"),
                    candidates=[
                        FieldCandidate(
                            value=c.get("value", ""),
                            method=c.get("method", "UNKNOWN"),
                            confidence=c.get("confidence", 0.0),
                            evidence_bbox=c.get("evidence_bbox"),
                        )
                        for c in candidates
                    ],
                    is_flagged=field_key in flagged_map,
                    flag_reason=flagged_map.get(field_key),
                ))

    db.commit()

    return ReviewPacketResponse(
        fax_job_id=str(fax_job_id),
        pages=page_info,
        extracted_fields=extracted_fields,
        template_match=review.review_packet.get("template_match") if review.review_packet else None,
        review_reasons=review.review_reasons or [],
        created_at=review.created_at,
        flagged_fields=stored_flags,
    )


@router.post("/{fax_job_id}/review/claim", response_model=ClaimResponse)
def claim_review(
    http_request: Request,
    fax_job_id: UUID,
    request: ClaimRequest,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClaimResponse:
    """Claim a review for processing (with optimistic locking)."""
    reviewer_id = user.user_id
    # Prevent reviewer impersonation: body reviewer_id must match auth user.
    if request.reviewer_id and request.reviewer_id != reviewer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="reviewer_id must match authenticated user",
        )

    # Tenant isolation: verify the job belongs to the caller's tenant
    settings = get_settings()
    if settings.environment != "development":
        job_repo = FaxJobRepository(db)
        job = job_repo.get_by_id(fax_job_id)
        if job and job.tenant_id != user.tenant_id and not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot claim a review belonging to another tenant",
            )

    review_repo = ReviewRepository(db)
    review = review_repo.get_by_job(fax_job_id)

    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No review found for job: {fax_job_id}",
        )

    if review.is_submitted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Review already submitted",
        )

    if review.is_claimed and not review.is_expired:
        if review.claimed_by != reviewer_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Review already claimed by another reviewer",
            )

    # Optimistic locking: use a conditional UPDATE to prevent race conditions.
    # If two reviewers try to claim simultaneously, only one succeeds.
    expected_claimed_by = review.claimed_by if (review.is_claimed and not review.is_expired) else None
    claimed = review_repo.claim_review_atomic(
        review_id=review.review_id,
        reviewer_id=reviewer_id,
        expected_claimed_by=expected_claimed_by,
    )

    if not claimed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Review was claimed by another reviewer (concurrent claim)",
        )

    # Audit log before commit so claim + audit are in the same transaction
    audit = get_audit_logger(http_request, db)
    audit.log_review_claim(review.review_id, {"reviewer": reviewer_id})

    db.commit()
    db.refresh(review)

    return ClaimResponse(
        review_id=str(review.review_id),
        claimed_by=review.claimed_by,
        claimed_at=review.claimed_at,
        expires_at=review.claim_expires_at,
    )


@router.post("/{fax_job_id}/review/submit", response_model=SubmitResponse)
def submit_review(
    http_request: Request,
    fax_job_id: UUID,
    request: SubmitRequest,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubmitResponse:
    """Submit a completed review with corrections."""
    reviewer_id = user.user_id
    # Prevent reviewer impersonation: body reviewer_id must match auth user.
    if request.reviewer_id and request.reviewer_id != reviewer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="reviewer_id must match authenticated user",
        )

    # Load job and enforce tenant isolation.
    job_repo = FaxJobRepository(db)
    job = job_repo.get_by_id(fax_job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Fax job not found: {fax_job_id}",
        )

    # Tenant isolation: verify the job belongs to the caller's tenant
    settings = get_settings()
    if settings.environment != "development":
        if job.tenant_id != user.tenant_id and not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot submit a review belonging to another tenant",
            )

    review_repo = ReviewRepository(db)
    review = review_repo.get_by_job(fax_job_id)

    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No review found for job: {fax_job_id}",
        )

    if review.is_submitted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Review already submitted",
        )

    if review.is_expired:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Review claim has expired; reclaim review before submitting",
        )

    if not review.is_claimed or review.claimed_by != reviewer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Review not claimed by this reviewer",
        )

    extraction_repo = ExtractionRepository(db)
    extraction = extraction_repo.get_by_job(fax_job_id)
    normalized_corrections: list[dict[str, Any]] = []
    if request.corrected_fields:
        allowed_field_keys = _collect_allowed_correction_fields(
            db=db,
            job=job,
            extraction=extraction,
            review_packet=review.review_packet,
        )
        if not allowed_field_keys:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Unable to validate corrected fields for this review packet",
            )

        normalized_corrections = _normalize_and_validate_corrections(
            request.corrected_fields,
            allowed_field_keys,
        )

    # Store corrections
    corrections = {
        c["field_key"]: {
            "corrected_value": c["corrected_value"],
            "evidence_bbox": c["evidence_bbox"],
        }
        for c in normalized_corrections
    }

    # Submit the review
    submitted_review = review_repo.submit_review(review.review_id, reviewer_id, corrections)
    if not submitted_review:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Review submission failed due to concurrent update or expired claim",
        )

    # Audit log
    audit = get_audit_logger(http_request, db)
    audit.log_review_submit(
        submitted_review.review_id,
        {"reviewer": reviewer_id, "corrections_count": len(normalized_corrections)},
    )

    # Create feedback records for training
    feedback_repo = FeedbackRepository(db)

    for correction in normalized_corrections:
        original_value = None
        if extraction and extraction.extraction_json:
            field_data = extraction.extraction_json.get(correction["field_key"], {})
            if isinstance(field_data, dict):
                original_value = field_data.get("value")

        feedback_repo.create_correction(
            fax_job_id=fax_job_id,
            review_id=submitted_review.review_id,
            field_key=correction["field_key"],
            original_value=original_value,
            corrected_value=correction["corrected_value"],
            created_by=reviewer_id,
        )

    # Apply human corrections back to extraction_json (HITL — conf=1.0, method=HUMAN_REVIEW)
    if normalized_corrections:
        corrections_dict = {c["field_key"]: c["corrected_value"] for c in normalized_corrections}
        extraction_repo.apply_corrections(fax_job_id, corrections_dict)
        logger.info(
            "HITL: applied %d correction(s) to extraction_json [job=%s, reviewer=%s]",
            len(corrections_dict),
            str(fax_job_id)[:8],
            reviewer_id,
        )

    # Write training labels (fax_label_example) for LayoutLM fine-tuning
    try:
        from libs.shared.db.repositories.label_example_repo import LabelExampleRepository

        label_repo = LabelExampleRepository(db)
        page_repo = FaxPageRepository(db)

        pages = page_repo.get_by_job(fax_job_id)
        page_map = {p.page_number: p for p in pages if not p.is_cover_page}
        fallback_page = next(iter(page_map.values()), None) if page_map else None

        review_page_numbers: list[int] = []
        if review.review_packet and isinstance(review.review_packet, dict):
            packet_pages = review.review_packet.get("pages")
            if isinstance(packet_pages, list):
                for item in packet_pages:
                    if not isinstance(item, dict):
                        continue
                    try:
                        pnum = int(item.get("page_number"))
                    except (TypeError, ValueError):
                        continue
                    if pnum in page_map:
                        review_page_numbers.append(pnum)
        review_page_fallback = page_map.get(review_page_numbers[0]) if review_page_numbers else None

        for correction in normalized_corrections:
            target_page = None

            # 1) reviewer-supplied bbox page
            corr_page = _extract_page_number_from_bbox(correction.get("evidence_bbox"))
            if corr_page is not None:
                target_page = page_map.get(corr_page)

            # 2) original extraction bbox page
            if target_page is None and extraction and extraction.extraction_json:
                field_data = extraction.extraction_json.get(correction["field_key"], {})
                if isinstance(field_data, dict):
                    source_page = _extract_page_number_from_bbox(field_data.get("evidence_bbox"))
                    if source_page is not None:
                        target_page = page_map.get(source_page)

            # 3) first selected review page
            if target_page is None:
                target_page = review_page_fallback

            # 4) global first non-cover fallback
            if target_page is None:
                target_page = fallback_page

            if target_page:
                label_repo.create_label(
                    fax_job_id=fax_job_id,
                    fax_page_id=target_page.fax_page_id,
                    field_key=correction["field_key"],
                    ground_truth_value=correction["corrected_value"],
                    page_storage_key=target_page.page_storage_key or "",
                    ground_truth_bbox=correction["evidence_bbox"],
                    payer_name=job.payer_hint,   # PayerNameEnum
                    doc_type=job.doc_type,       # DocTypeEnum
                    source="human_review",
                    created_by=reviewer_id,
                )
    except Exception:
        logger.warning(
            "Failed to write training labels for job %s", fax_job_id, exc_info=True
        )

    # Update job status
    from libs.shared.db.models.enums import FaxJobStatusEnum
    # Review completion should not rewrite OCR pipeline completion timestamps.
    job.status = FaxJobStatusEnum.COMPLETED
    job.needs_review = False

    db.commit()
    db.refresh(submitted_review)

    return SubmitResponse(
        review_id=str(submitted_review.review_id),
        submitted_by=submitted_review.submitted_by,
        submitted_at=submitted_review.submitted_at,
        corrections_count=len(normalized_corrections),
    )


@router.get("/reviews/pending", response_model=list[ReviewListItem])
def list_pending_reviews(
    http_request: Request,
    user: AuthUser = Depends(get_current_user),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[ReviewListItem]:
    """List all pending reviews (scoped to caller's tenant unless admin)."""
    limit = min(limit, 500)
    # Admins see all tenants; regular users see only their own
    tenant_filter = None if user.is_admin else user.tenant_id
    review_repo = ReviewRepository(db)
    reviews = review_repo.get_pending_reviews(skip, limit, tenant_id=tenant_filter)
    audit = get_audit_logger(http_request, db)
    audit.log_read("review_list_pending", None, {"count": len(reviews), "tenant_id": tenant_filter})
    db.commit()

    return [
        ReviewListItem(
            review_id=str(r.review_id),
            fax_job_id=str(r.fax_job_id),
            priority=r.priority,
            review_reasons=r.review_reasons or [],
            claimed_by=r.claimed_by,
            created_at=r.created_at,
        )
        for r in reviews
    ]


@router.get("/reviews/unclaimed", response_model=list[ReviewListItem])
def list_unclaimed_reviews(
    http_request: Request,
    user: AuthUser = Depends(get_current_user),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[ReviewListItem]:
    """List unclaimed reviews available for claiming (scoped to caller's tenant unless admin)."""
    limit = min(limit, 500)
    # Admins see all tenants; regular users see only their own
    tenant_filter = None if user.is_admin else user.tenant_id
    review_repo = ReviewRepository(db)
    reviews = review_repo.get_unclaimed_reviews(skip, limit, tenant_id=tenant_filter)
    audit = get_audit_logger(http_request, db)
    audit.log_read("review_list_unclaimed", None, {"count": len(reviews), "tenant_id": tenant_filter})
    db.commit()

    return [
        ReviewListItem(
            review_id=str(r.review_id),
            fax_job_id=str(r.fax_job_id),
            priority=r.priority,
            review_reasons=r.review_reasons or [],
            claimed_by=None,
            created_at=r.created_at,
        )
        for r in reviews
    ]


@router.post("/reviews/release-expired")
def release_expired_claims(
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Release all expired claims (admin only)."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    review_repo = ReviewRepository(db)
    count = review_repo.release_expired_claims()
    db.commit()

    return {"released_count": count}
