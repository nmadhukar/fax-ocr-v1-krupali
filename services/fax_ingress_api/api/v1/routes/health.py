"""
Health check endpoints.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from libs.shared.config import get_settings
from libs.shared.db.session import get_db

router = APIRouter()


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    timestamp: datetime
    version: str
    checks: dict[str, Any]


@router.get("/health", response_model=HealthResponse)
def health_check(db: Session = Depends(get_db)) -> HealthResponse:
    """
    Perform health check on all dependencies.

    Returns:
        HealthResponse with status of each dependency.
    """
    settings = get_settings()
    checks: dict[str, Any] = {}

    # Database check
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {"status": "healthy"}
    except Exception as e:
        # Never expose raw DB errors (may contain connection strings/passwords)
        checks["database"] = {
            "status": "unhealthy",
            "error": str(e) if settings.api.debug else "database connection failed",
        }

    # Overall status
    all_healthy = all(
        c.get("status") == "healthy" for c in checks.values()
    )

    return HealthResponse(
        status="healthy" if all_healthy else "degraded",
        timestamp=datetime.utcnow(),
        version=settings.app_version,
        checks=checks,
    )


@router.get("/ready")
def readiness_check(db: Session = Depends(get_db)) -> dict[str, str]:
    """
    Kubernetes readiness probe.

    Returns 200 if service is ready to accept traffic.
    """
    # Check database
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get("/live")
def liveness_check() -> dict[str, str]:
    """
    Kubernetes liveness probe.

    Returns 200 if service is alive.
    """
    return {"status": "alive"}
