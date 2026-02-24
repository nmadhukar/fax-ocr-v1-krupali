"""
Template management endpoints.
"""

import io
import logging
import re
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from libs.shared.db.models.enums import DocTypeEnum, PayerNameEnum
from libs.shared.db.models.fax_template import (
    FaxTemplate,
    FaxTemplateField,
    FaxTemplateSample,
    FaxTemplateVersion,
)
from libs.shared.db.repositories.fax_page_repo import FaxPageRepository
from libs.shared.db.repositories.ocr_token_repo import OcrTokenRepository
from libs.shared.db.repositories.template_repo import (
    TemplateFieldRepository,
    TemplateRepository,
    TemplateSampleRepository,
    TemplateVersionRepository,
)
from libs.shared.db.session import get_db
from libs.shared.security.auth import AuthUser, get_current_user
from libs.shared.storage.s3_adapter import S3StorageAdapter
from libs.shared.template.matcher import TemplateMatcher

logger = logging.getLogger(__name__)

router = APIRouter()


# Pydantic schemas
class TemplateCreate(BaseModel):
    """Request to create a template."""

    payer_name: str
    doc_type: str
    template_name: str
    description: str | None = None


class TemplateResponse(BaseModel):
    """Template response."""

    template_id: UUID
    payer_name: str
    doc_type: str
    template_name: str
    description: str | None
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class VersionCreate(BaseModel):
    """Request to create a template version."""

    version_label: str
    match_min_score: float = 0.75
    match_phash_threshold: int = 10
    match_orb_min_matches: int = 20


class VersionUpdate(BaseModel):
    """Request to update matching thresholds on an existing template version."""

    match_min_score: float | None = None
    match_phash_threshold: int | None = None
    match_orb_min_matches: int | None = None


class VersionResponse(BaseModel):
    """Template version response."""

    template_version_id: UUID
    template_id: UUID
    version_label: str
    match_min_score: float
    match_phash_threshold: int
    match_orb_min_matches: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class FieldCreate(BaseModel):
    """Request to create a template field."""

    field_key: str
    field_label: str | None = None
    is_required: bool = False
    roi_x0: float = Field(..., ge=0.0, le=1.0)
    roi_y0: float = Field(..., ge=0.0, le=1.0)
    roi_x1: float = Field(..., ge=0.0, le=1.0)
    roi_y1: float = Field(..., ge=0.0, le=1.0)
    target_page: int = 1
    validation_regex: str | None = None
    expected_type: str = "text"


class FieldResponse(BaseModel):
    """Template field response."""

    template_field_id: UUID
    field_key: str
    field_label: str | None
    is_required: bool
    roi_x0: float
    roi_y0: float
    roi_x1: float
    roi_y1: float
    target_page: int
    validation_regex: str | None
    expected_type: str

    class Config:
        from_attributes = True


class SampleResponse(BaseModel):
    """Template sample response."""

    sample_id: UUID
    sample_storage_key: str
    width_px: int
    height_px: int
    created_at: datetime

    class Config:
        from_attributes = True


class TestMatchRequest(BaseModel):
    """Request for test matching."""

    payer_hint: str | None = None
    doc_type: str | None = None


class TestMatchResponse(BaseModel):
    """Response for test matching."""

    matched: bool
    template_version_id: UUID | None
    template_name: str | None
    payer_name: str | None
    match_score: float
    phash_distance: int | None
    orb_matches: int
    orb_inliers: int


class SuggestRoiRequest(BaseModel):
    """Request for ROI suggestion."""

    fax_job_id: UUID
    page_number: int
    field_key: str
    correct_value: str | None = None


class SuggestRoiResponse(BaseModel):
    """Response for ROI suggestion."""

    suggested_roi: dict[str, float] | None
    evidence_text: str | None
    confidence: float


class TestExtractFieldResult(BaseModel):
    """Single field extraction result."""

    field_key: str
    value: str | None
    confidence: float
    evidence_bbox: dict[str, Any] | None = None
    evidence_text: str | None = None


class TestExtractResponse(BaseModel):
    """Response for test extraction."""

    version_id: str
    fields: list[TestExtractFieldResult]
    total_fields: int


def get_storage() -> S3StorageAdapter:
    """Dependency for storage adapter."""
    return S3StorageAdapter()


def _require_admin(user: AuthUser) -> None:
    """Enforce admin-only access for template management."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


# Template endpoints
@router.post("", response_model=TemplateResponse, status_code=status.HTTP_201_CREATED)
def create_template(
    data: TemplateCreate,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TemplateResponse:
    """Create a new template."""
    _require_admin(user)

    # Validate enums
    try:
        payer_enum = PayerNameEnum(data.payer_name.upper())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid payer_name: {data.payer_name}",
        )

    try:
        doc_enum = DocTypeEnum(data.doc_type.upper())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid doc_type: {data.doc_type}",
        )

    repo = TemplateRepository(db)

    template = FaxTemplate(
        payer_name=payer_enum,
        doc_type=doc_enum,
        template_name=data.template_name,
        description=data.description,
    )

    repo.create(template)
    db.commit()

    return TemplateResponse(
        template_id=template.template_id,
        payer_name=template.payer_name.value,
        doc_type=template.doc_type.value,
        template_name=template.template_name,
        description=template.description,
        is_active=template.is_active,
        created_at=template.created_at,
    )


@router.get("", response_model=list[TemplateResponse])
def list_templates(
    payer_name: str | None = None,
    active_only: bool = False,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TemplateResponse]:
    """List templates (active and inactive by default)."""
    _require_admin(user)

    repo = TemplateRepository(db)

    payer_enum = None
    if payer_name:
        try:
            payer_enum = PayerNameEnum(payer_name.upper())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid payer_name: {payer_name}",
            )

    templates = repo.list_templates(payer_enum, active_only=active_only)

    return [
        TemplateResponse(
            template_id=t.template_id,
            payer_name=t.payer_name.value,
            doc_type=t.doc_type.value,
            template_name=t.template_name,
            description=t.description,
            is_active=t.is_active,
            created_at=t.created_at,
        )
        for t in templates
    ]


@router.get("/{template_id}", response_model=TemplateResponse)
def get_template(
    template_id: UUID,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TemplateResponse:
    """Get template details."""
    _require_admin(user)

    repo = TemplateRepository(db)
    template = repo.get_by_id(template_id)

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}",
        )

    return TemplateResponse(
        template_id=template.template_id,
        payer_name=template.payer_name.value,
        doc_type=template.doc_type.value,
        template_name=template.template_name,
        description=template.description,
        is_active=template.is_active,
        created_at=template.created_at,
    )


class TemplateUpdate(BaseModel):
    """Request to update a template."""

    template_name: str | None = None
    description: str | None = None
    is_active: bool | None = None


@router.put("/{template_id}", response_model=TemplateResponse)
def update_template(
    template_id: UUID,
    data: TemplateUpdate,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TemplateResponse:
    """Update a template's metadata."""
    _require_admin(user)

    repo = TemplateRepository(db)
    template = repo.get_by_id(template_id)

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}",
        )

    update_data = data.model_dump(exclude_unset=True)
    if update_data:
        repo.update(template, update_data)
        db.commit()
        db.refresh(template)

    return TemplateResponse(
        template_id=template.template_id,
        payer_name=template.payer_name.value,
        doc_type=template.doc_type.value,
        template_name=template.template_name,
        description=template.description,
        is_active=template.is_active,
        created_at=template.created_at,
    )


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    template_id: UUID,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Delete a template and all its versions, samples, and fields (cascade)."""
    _require_admin(user)

    repo = TemplateRepository(db)
    template = repo.get_by_id(template_id)

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}",
        )

    repo.delete(template)
    db.commit()
    logger.info("Deleted template %s (%s)", template_id, template.template_name)


# Version endpoints
@router.post("/{template_id}/versions", response_model=VersionResponse, status_code=status.HTTP_201_CREATED)
def create_version(
    template_id: UUID,
    data: VersionCreate,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VersionResponse:
    """Create a new template version."""
    _require_admin(user)

    template_repo = TemplateRepository(db)
    template = template_repo.get_by_id(template_id)

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}",
        )

    version_repo = TemplateVersionRepository(db)

    version = FaxTemplateVersion(
        template_id=template_id,
        version_label=data.version_label,
        match_min_score=data.match_min_score,
        match_phash_threshold=data.match_phash_threshold,
        match_orb_min_matches=data.match_orb_min_matches,
    )

    version_repo.create(version)
    db.commit()

    return VersionResponse(
        template_version_id=version.template_version_id,
        template_id=version.template_id,
        version_label=version.version_label,
        match_min_score=float(version.match_min_score),
        match_phash_threshold=version.match_phash_threshold,
        match_orb_min_matches=version.match_orb_min_matches,
        is_active=version.is_active,
        created_at=version.created_at,
    )


@router.post("/versions/{version_id}/activate", response_model=VersionResponse)
def activate_version(
    version_id: UUID,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VersionResponse:
    """Activate a template version."""
    _require_admin(user)

    repo = TemplateVersionRepository(db)
    version = repo.activate_version(version_id)

    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version not found: {version_id}",
        )

    db.commit()

    return VersionResponse(
        template_version_id=version.template_version_id,
        template_id=version.template_id,
        version_label=version.version_label,
        match_min_score=float(version.match_min_score),
        match_phash_threshold=version.match_phash_threshold,
        match_orb_min_matches=version.match_orb_min_matches,
        is_active=version.is_active,
        created_at=version.created_at,
    )


@router.put("/versions/{version_id}", response_model=VersionResponse)
def update_version_thresholds(
    version_id: UUID,
    data: VersionUpdate,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VersionResponse:
    """Update matching thresholds for a template version (match_min_score, phash_threshold, orb_min_matches)."""
    _require_admin(user)

    repo = TemplateVersionRepository(db)
    version = repo.get_by_id(version_id)

    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version not found: {version_id}",
        )

    updates = {k: v for k, v in data.model_dump().items() if v is not None}
    if updates:
        repo.update(version, updates)
        db.commit()

    return VersionResponse(
        template_version_id=version.template_version_id,
        template_id=version.template_id,
        version_label=version.version_label,
        match_min_score=float(version.match_min_score),
        match_phash_threshold=version.match_phash_threshold,
        match_orb_min_matches=version.match_orb_min_matches,
        is_active=version.is_active,
        created_at=version.created_at,
    )


# Sample endpoints
@router.post("/versions/{version_id}/samples", response_model=SampleResponse, status_code=status.HTTP_201_CREATED)
def upload_sample(
    version_id: UUID,
    file: Annotated[UploadFile, File(description="Sample image")],
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_storage),
) -> SampleResponse:
    """Upload a template sample image."""
    _require_admin(user)

    version_repo = TemplateVersionRepository(db)
    version = version_repo.get_by_id(version_id)

    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version not found: {version_id}",
        )

    # Read image with size limit (sync — FastAPI runs sync endpoints in threadpool)
    MAX_SAMPLE_SIZE = 10 * 1024 * 1024  # 10 MB
    content = file.file.read()
    if len(content) > MAX_SAMPLE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Sample image exceeds {MAX_SAMPLE_SIZE // (1024*1024)} MB limit",
        )

    # Validate image content type
    allowed_types = {"image/png", "image/jpeg", "image/tiff", "image/bmp"}
    if file.content_type and file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid content type: {file.content_type}. Allowed: {', '.join(allowed_types)}",
        )

    nparr = np.frombuffer(content, np.uint8)
    image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if image is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image file",
        )

    height, width = image.shape[:2]

    # Compute features (cv2/ORB can raise on corrupted images)
    matcher = TemplateMatcher()
    try:
        phash, kp_bytes, desc_bytes = matcher.compute_template_features(image)
    except Exception as e:
        logger.warning("Feature computation failed for sample upload: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to compute image features — image may be corrupted",
        )

    # Store image — sanitize the user-supplied filename
    safe_name = re.sub(r"[^\w.\-]", "_", (file.filename or "sample.png").split("/")[-1].split("\\")[-1])
    unique_prefix = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S%f')}_{uuid4().hex[:8]}"
    storage_key = f"templates/{version_id}/{unique_prefix}_{safe_name}"
    storage.upload(storage_key, content, content_type=file.content_type or "image/png")

    # Create sample record
    sample_repo = TemplateSampleRepository(db)
    sample = FaxTemplateSample(
        template_version_id=version_id,
        sample_storage_key=storage_key,
        phash_value=phash,
        orb_keypoints=kp_bytes,
        orb_descriptors=desc_bytes,
        width_px=width,
        height_px=height,
    )

    sample_repo.create(sample)
    db.commit()

    return SampleResponse(
        sample_id=sample.sample_id,
        sample_storage_key=sample.sample_storage_key,
        width_px=sample.width_px,
        height_px=sample.height_px,
        created_at=sample.created_at,
    )


# Field endpoints
@router.post("/versions/{version_id}/fields", response_model=FieldResponse, status_code=status.HTTP_201_CREATED)
def create_field(
    version_id: UUID,
    data: FieldCreate,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FieldResponse:
    """Create a template field definition."""
    _require_admin(user)

    version_repo = TemplateVersionRepository(db)
    version = version_repo.get_by_id(version_id)

    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version not found: {version_id}",
        )

    field_repo = TemplateFieldRepository(db)

    field = FaxTemplateField(
        template_version_id=version_id,
        field_key=data.field_key,
        field_label=data.field_label,
        is_required=data.is_required,
        roi_x0=data.roi_x0,
        roi_y0=data.roi_y0,
        roi_x1=data.roi_x1,
        roi_y1=data.roi_y1,
        target_page=data.target_page,
        validation_regex=data.validation_regex,
        expected_type=data.expected_type,
    )

    field_repo.create(field)
    db.commit()

    return FieldResponse(
        template_field_id=field.template_field_id,
        field_key=field.field_key,
        field_label=field.field_label,
        is_required=field.is_required,
        roi_x0=float(field.roi_x0),
        roi_y0=float(field.roi_y0),
        roi_x1=float(field.roi_x1),
        roi_y1=float(field.roi_y1),
        target_page=field.target_page,
        validation_regex=field.validation_regex,
        expected_type=field.expected_type,
    )


@router.get("/versions/{version_id}/fields", response_model=list[FieldResponse])
def list_fields(
    version_id: UUID,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[FieldResponse]:
    """List all fields for a version."""
    _require_admin(user)

    repo = TemplateFieldRepository(db)
    fields = repo.get_by_version(version_id)

    return [
        FieldResponse(
            template_field_id=f.template_field_id,
            field_key=f.field_key,
            field_label=f.field_label,
            is_required=f.is_required,
            roi_x0=float(f.roi_x0),
            roi_y0=float(f.roi_y0),
            roi_x1=float(f.roi_x1),
            roi_y1=float(f.roi_y1),
            target_page=f.target_page,
            validation_regex=f.validation_regex,
            expected_type=f.expected_type,
        )
        for f in fields
    ]


# Test match endpoint
@router.post("/test-match", response_model=TestMatchResponse)
def test_match(
    file: Annotated[UploadFile, File(description="Image to test")],
    payer_hint: str | None = None,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TestMatchResponse:
    """Test template matching against uploaded image."""
    _require_admin(user)

    MAX_TEST_SIZE = 10 * 1024 * 1024  # 10 MB
    content = file.file.read()
    if len(content) > MAX_TEST_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Image exceeds {MAX_TEST_SIZE // (1024*1024)} MB limit",
        )

    nparr = np.frombuffer(content, np.uint8)
    image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if image is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image file",
        )

    # Validate payer_hint against known enum values
    validated_payer_hint = None
    if payer_hint:
        try:
            PayerNameEnum(payer_hint.upper())
            validated_payer_hint = payer_hint
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid payer_hint: {payer_hint}",
            )

    matcher = TemplateMatcher()
    result = matcher.match(image, db, validated_payer_hint)

    return TestMatchResponse(
        matched=result.matched,
        template_version_id=result.template_version_id,
        template_name=result.template_name,
        payer_name=result.payer_name,
        match_score=result.score,
        phash_distance=result.phash_distance,
        orb_matches=result.orb_matches,
        orb_inliers=result.orb_inliers,
    )


# Suggest ROI endpoint
@router.post("/suggest-roi", response_model=SuggestRoiResponse)
def suggest_roi(
    data: SuggestRoiRequest,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SuggestRoiResponse:
    """
    Suggest a bounding box ROI for a field based on OCR evidence.

    Searches OCR tokens on the specified page to find where the field
    value appears, then returns the union bounding box with padding
    as a suggested ROI for template field definition.

    Args:
        data: Request with fax_job_id, page_number, field_key, and
              optional correct_value to search for.

    Returns:
        SuggestRoiResponse with suggested ROI coordinates.
    """
    _require_admin(user)

    page_repo = FaxPageRepository(db)
    page = page_repo.get_page_with_tokens(data.fax_job_id, data.page_number)

    if not page:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Page {data.page_number} not found for job {data.fax_job_id}",
        )

    token_repo = OcrTokenRepository(db)
    tokens = token_repo.get_by_page(page.fax_page_id)

    if not tokens:
        return SuggestRoiResponse(
            suggested_roi=None,
            evidence_text=None,
            confidence=0.0,
        )

    # If a correct value is provided, search for it in OCR tokens
    if data.correct_value:
        matched_tokens = _find_value_in_tokens(
            data.correct_value, tokens
        )
    else:
        # Without a value, use field_key heuristics to find likely tokens
        matched_tokens = _find_field_by_label(
            data.field_key, tokens
        )

    if not matched_tokens:
        return SuggestRoiResponse(
            suggested_roi=None,
            evidence_text=None,
            confidence=0.0,
        )

    # Compute union bbox with padding
    roi_padding = 0.01  # 1% padding on each side
    suggested_roi = {
        "x0": max(0.0, min(float(t.bbox_x0) for t in matched_tokens) - roi_padding),
        "y0": max(0.0, min(float(t.bbox_y0) for t in matched_tokens) - roi_padding),
        "x1": min(1.0, max(float(t.bbox_x1) for t in matched_tokens) + roi_padding),
        "y1": min(1.0, max(float(t.bbox_y1) for t in matched_tokens) + roi_padding),
    }

    evidence_text = " ".join(t.token_text for t in matched_tokens)
    avg_conf = sum(float(t.confidence) for t in matched_tokens) / len(matched_tokens)

    logger.info(
        "Suggest ROI for '%s': found %d tokens, confidence=%.2f",
        data.field_key,
        len(matched_tokens),
        avg_conf,
    )

    return SuggestRoiResponse(
        suggested_roi=suggested_roi,
        evidence_text=evidence_text,
        confidence=round(avg_conf, 4),
    )


def _find_value_in_tokens(
    value: str,
    tokens: list,
) -> list:
    """
    Find OCR tokens that match a given value using fuzzy matching.

    Splits the value into words and matches each word to the closest
    OCR token using Levenshtein distance.
    """
    try:
        from Levenshtein import distance as lev_distance
    except ImportError:
        def lev_distance(a: str, b: str) -> int:
            m, n = len(a), len(b)
            dp = list(range(n + 1))
            for i in range(1, m + 1):
                prev = dp[0]
                dp[0] = i
                for j in range(1, n + 1):
                    temp = dp[j]
                    dp[j] = prev if a[i - 1] == b[j - 1] else 1 + min(prev, dp[j], dp[j - 1])
                    prev = temp
            return dp[n]

    value_words = value.strip().upper().split()
    if not value_words:
        return []

    matched = []
    threshold = 2  # Max edit distance

    for word in value_words:
        best_token = None
        best_dist = threshold + 1

        for token in tokens:
            token_text = token.token_text.strip().upper()
            if not token_text:
                continue
            dist = lev_distance(word, token_text)
            if dist < best_dist:
                best_dist = dist
                best_token = token

        if best_token is not None and best_dist <= threshold:
            matched.append(best_token)

    return matched


def _find_field_by_label(
    field_key: str,
    tokens: list,
) -> list:
    """
    Find OCR tokens near a field label.

    Looks for the field label in OCR tokens, then returns
    tokens to the right of or below the label (likely the value).
    """
    # Convert field_key to label-like text
    label_words = field_key.replace("_", " ").upper().split()

    # Find label tokens
    label_tokens = []
    for word in label_words:
        for token in tokens:
            if token.token_text.strip().upper() == word:
                label_tokens.append(token)
                break

    if not label_tokens:
        return []

    # Find value tokens: to the right of or just below the label
    label_x1 = max(float(t.bbox_x1) for t in label_tokens)
    label_y0 = min(float(t.bbox_y0) for t in label_tokens)
    label_y1 = max(float(t.bbox_y1) for t in label_tokens)
    row_height = label_y1 - label_y0

    value_tokens = []
    for token in tokens:
        if token in label_tokens:
            continue
        tx0 = float(token.bbox_x0)
        ty0 = float(token.bbox_y0)

        # Token is to the right on the same line
        same_line = abs(ty0 - label_y0) < row_height * 0.5
        to_right = tx0 >= label_x1 - 0.01

        # Token is on the next line, roughly same x range
        next_line = (label_y1 - 0.01) <= ty0 <= (label_y1 + row_height * 2)

        if (same_line and to_right) or next_line:
            value_tokens.append(token)
            if len(value_tokens) >= 5:
                break

    return value_tokens if value_tokens else label_tokens


# Test extract endpoint
@router.post(
    "/versions/{version_id}/test-extract",
    response_model=TestExtractResponse,
)
def test_extract(
    version_id: UUID,
    file: Annotated[UploadFile, File(description="Page image to test extraction")],
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TestExtractResponse:
    """Test ROI-based field extraction against a template version.

    Upload a page image and run the template's ROI fields against it
    to preview what values would be extracted.
    """
    _require_admin(user)

    version_repo = TemplateVersionRepository(db)
    version = version_repo.get_by_id(version_id)

    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version not found: {version_id}",
        )

    MAX_TEST_SIZE = 10 * 1024 * 1024  # 10 MB
    content = file.file.read()
    if len(content) > MAX_TEST_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Image exceeds {MAX_TEST_SIZE // (1024*1024)} MB limit",
        )
    nparr = np.frombuffer(content, np.uint8)
    image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if image is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image file",
        )

    # Run OCR on the image
    from libs.shared.ocr.paddle_client import PaddleOcrClient
    from libs.shared.utils.image_utils import ImagePreprocessor

    preprocessor = ImagePreprocessor()
    processed, _ = preprocessor.preprocess(image)
    ocr_client = PaddleOcrClient()
    ocr_result = ocr_client.extract(processed)

    # Get template fields for this version
    field_repo = TemplateFieldRepository(db)
    fields = field_repo.get_by_version(version_id)

    if not fields:
        return TestExtractResponse(
            version_id=str(version_id),
            fields=[],
            total_fields=0,
        )

    height, width = image.shape[:2]
    results: list[TestExtractFieldResult] = []

    for field_def in fields:
        # Extract ROI coordinates (normalised 0-1)
        x0 = float(field_def.roi_x0)
        y0 = float(field_def.roi_y0)
        x1 = float(field_def.roi_x1)
        y1 = float(field_def.roi_y1)

        # Find OCR tokens within this ROI
        matched_tokens = []
        for token in ocr_result.tokens:
            # Token bbox is normalised
            t_cx = (token.bbox[0] + token.bbox[2]) / 2
            t_cy = (token.bbox[1] + token.bbox[3]) / 2
            if x0 <= t_cx <= x1 and y0 <= t_cy <= y1:
                matched_tokens.append(token)

        if matched_tokens:
            value = " ".join(t.text for t in matched_tokens)
            avg_conf = sum(t.confidence for t in matched_tokens) / len(matched_tokens)
            evidence_text = value
            evidence_bbox = {
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "page": 1,
            }
        else:
            value = None
            avg_conf = 0.0
            evidence_text = None
            evidence_bbox = None

        results.append(TestExtractFieldResult(
            field_key=field_def.field_key,
            value=value,
            confidence=round(avg_conf, 4),
            evidence_bbox=evidence_bbox,
            evidence_text=evidence_text,
        ))

    return TestExtractResponse(
        version_id=str(version_id),
        fields=results,
        total_fields=len(results),
    )
