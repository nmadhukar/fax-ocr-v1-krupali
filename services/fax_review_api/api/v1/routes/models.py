"""
Model version management endpoints.

Client requirement: "model_version table (model_type, version_tag, active, metrics_json, promoted_at)"
"""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from libs.shared.db.repositories.model_version_repo import ModelVersionRepository
from libs.shared.db.session import get_db
from libs.shared.security.auth import AuthUser, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


# --- Pydantic schemas ---

class ModelVersionCreate(BaseModel):
    """Register a new model version."""

    model_type: str = Field(..., description="Model type: OCR, VLM, LAYOUTLM")
    version_tag: str = Field(..., description="Version identifier e.g. 'layoutlm-v1.0'")
    model_path: str | None = Field(None, description="Filesystem or HF model path")
    notes: str | None = None
    config: dict[str, Any] | None = None


class ModelVersionResponse(BaseModel):
    """Model version response."""

    model_version_id: UUID
    model_type: str
    version_tag: str
    model_path: str | None
    is_active: bool
    metrics_json: dict[str, Any]
    config_json: dict[str, Any]
    promoted_at: datetime | None
    promoted_by: str | None
    created_at: datetime
    notes: str | None

    class Config:
        from_attributes = True


class PromoteRequest(BaseModel):
    """Request to promote a model version."""

    promoted_by: str = "admin"


class MetricsUpdate(BaseModel):
    """Update metrics for a model version."""

    metrics: dict[str, Any]


# --- Endpoints ---

@router.get("", response_model=list[ModelVersionResponse])
def list_model_versions(
    model_type: str | None = None,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ModelVersionResponse]:
    """List model versions, optionally filtered by type."""
    repo = ModelVersionRepository(db)
    if model_type:
        versions = repo.get_by_type(model_type.upper())
    else:
        versions = repo.get_all()
    return [_to_response(v) for v in versions]


@router.get("/active/{model_type}", response_model=ModelVersionResponse)
def get_active_version(
    model_type: str,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ModelVersionResponse:
    """Get the currently active version for a model type."""
    repo = ModelVersionRepository(db)
    version = repo.get_active(model_type.upper())
    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active version for model_type={model_type}",
        )
    return _to_response(version)


@router.post("", response_model=ModelVersionResponse, status_code=status.HTTP_201_CREATED)
def register_model_version(
    data: ModelVersionCreate,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ModelVersionResponse:
    """Register a new model version (inactive by default)."""
    repo = ModelVersionRepository(db)
    version = repo.register_version(
        model_type=data.model_type.upper(),
        version_tag=data.version_tag,
        model_path=data.model_path,
        notes=data.notes,
        config=data.config,
    )
    db.commit()
    logger.info("Registered model version: %s/%s", data.model_type, data.version_tag)
    return _to_response(version)


@router.post("/{model_version_id}/promote", response_model=ModelVersionResponse)
def promote_model_version(
    model_version_id: UUID,
    data: PromoteRequest,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ModelVersionResponse:
    """Promote a model version to active (deactivates others of same type)."""
    repo = ModelVersionRepository(db)
    version = repo.promote(model_version_id, promoted_by=data.promoted_by)
    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model version not found: {model_version_id}",
        )
    db.commit()
    logger.info(
        "Promoted model version %s (%s/%s) by %s",
        model_version_id, version.model_type, version.version_tag, data.promoted_by,
    )
    return _to_response(version)


@router.put("/{model_version_id}/metrics", response_model=ModelVersionResponse)
def update_model_metrics(
    model_version_id: UUID,
    data: MetricsUpdate,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ModelVersionResponse:
    """Update metrics for a model version."""
    repo = ModelVersionRepository(db)
    version = repo.update_metrics(model_version_id, data.metrics)
    if not version:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model version not found: {model_version_id}",
        )
    db.commit()
    return _to_response(version)


@router.delete("/{model_version_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model_version(
    model_version_id: UUID,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Delete a model version."""
    repo = ModelVersionRepository(db)
    deleted = repo.delete_by_id(model_version_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model version not found: {model_version_id}",
        )
    db.commit()


def _to_response(v) -> ModelVersionResponse:
    """Convert ModelVersion to response model."""
    return ModelVersionResponse(
        model_version_id=v.model_version_id,
        model_type=v.model_type,
        version_tag=v.version_tag,
        model_path=v.model_path,
        is_active=v.is_active,
        metrics_json=v.metrics_json or {},
        config_json=v.config_json or {},
        promoted_at=v.promoted_at,
        promoted_by=v.promoted_by,
        created_at=v.created_at,
        notes=v.notes,
    )
