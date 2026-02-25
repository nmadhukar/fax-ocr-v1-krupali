"""
Quality analytics and feedback endpoints.

Provides dashboard metrics, per-payer stats, feedback summaries,
confidence recalibration triggers, and adaptive template-drift signals.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from libs.shared.db.repositories.analytics_repo import AnalyticsRepository
from libs.shared.db.session import get_db
from libs.shared.security.auth import AuthUser, get_current_user

router = APIRouter()


def _require_admin(user: AuthUser) -> None:
    """Enforce admin-only access for analytics endpoints."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


@router.get("/quality")
def get_quality_metrics(
    days: int = Query(default=30, ge=1, le=365, description="Lookback period in days"),
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Get overall quality metrics (admin only).

    Returns processing counts, auto-finalize rate, average confidence,
    average processing time, and doc type distribution.
    """
    _require_admin(user)

    repo = AnalyticsRepository(db)
    overview = repo.get_quality_overview(days=days)
    processing_times = repo.get_processing_time_stats(days=days)
    doc_types = repo.get_doc_type_distribution(days=days)

    return {
        **overview,
        "processing_times": processing_times,
        "doc_type_distribution": doc_types,
    }


@router.get("/payer/{payer_name}")
def get_payer_stats(
    payer_name: str,
    days: int = Query(default=30, ge=1, le=365),
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Get per-payer processing stats and common corrections."""
    _require_admin(user)

    repo = AnalyticsRepository(db)
    tenant_id = None if user.is_admin else user.tenant_id
    stats = repo.get_payer_stats(payer_name=payer_name, days=days, tenant_id=tenant_id)
    corrections = repo.get_corrections_by_payer(payer_name=payer_name, days=days, tenant_id=tenant_id)

    return {
        "payer": payer_name.upper(),
        "stats": stats[0] if stats else None,
        "common_corrections": corrections,
    }


@router.get("/payers")
def get_all_payer_stats(
    days: int = Query(default=30, ge=1, le=365),
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Get stats for all payers."""
    _require_admin(user)

    repo = AnalyticsRepository(db)
    tenant_id = None if user.is_admin else user.tenant_id
    return repo.get_payer_stats(days=days, tenant_id=tenant_id)


@router.get("/feedback-summary")
def get_feedback_summary(
    days: int = Query(default=30, ge=1, le=365),
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Get feedback and correction summary with most-corrected fields."""
    _require_admin(user)

    repo = AnalyticsRepository(db)
    tenant_id = None if user.is_admin else user.tenant_id
    summary = repo.get_feedback_summary(days=days, tenant_id=tenant_id)
    common_corrections = repo.get_common_corrections(days=days, tenant_id=tenant_id)

    return {
        **summary,
        "common_corrections": common_corrections,
    }


@router.post("/recalibrate")
def trigger_recalibration(
    days: int = Query(default=30, ge=1, le=365),
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Trigger confidence threshold recalibration based on feedback data (admin only).

    Analyzes correction history and recommends per-payer,
    per-field confidence threshold adjustments.
    """
    _require_admin(user)

    from libs.shared.feedback.analyzer import FeedbackAnalyzer

    analyzer = FeedbackAnalyzer(db)
    recommendations = analyzer.analyze_and_recommend(days=days)

    return {
        "status": "completed",
        "period_days": days,
        "recommendations": recommendations,
    }


@router.get("/template-drift")
def get_template_drift(
    days: int = Query(default=30, ge=1, le=365, description="Lookback period in days"),
    limit: int = Query(default=100, ge=1, le=500, description="Max recent events to return"),
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Get template drift and discovery telemetry from audit events (admin only).

    Reads adaptive extraction monitor events from ``audit_log`` and returns:
    - drift alert count
    - discovery cluster count
    - recent event payloads
    """
    _require_admin(user)

    since = datetime.now(timezone.utc) - timedelta(days=days)

    counts_sql = text(
        """
        SELECT
            resource_type,
            COUNT(*) AS total
        FROM audit_log
        WHERE action = 'PROCESS'
          AND resource_type IN ('template_drift_alert', 'template_discovery_cluster')
          AND created_at >= :since
        GROUP BY resource_type
        """
    )
    count_rows = db.execute(counts_sql, {"since": since}).fetchall()

    counts: dict[str, int] = {str(r.resource_type): int(r.total or 0) for r in count_rows}

    recent_sql = text(
        """
        SELECT created_at, resource_type, details
        FROM audit_log
        WHERE action = 'PROCESS'
          AND resource_type IN ('template_drift_alert', 'template_discovery_cluster')
          AND created_at >= :since
        ORDER BY created_at DESC
        LIMIT :limit
        """
    )
    recent_rows = db.execute(recent_sql, {"since": since, "limit": limit}).fetchall()

    recent_events = [
        {
            "created_at": (
                row.created_at.isoformat()
                if getattr(row, "created_at", None) is not None
                else None
            ),
            "event_type": str(row.resource_type),
            "details": row.details or {},
        }
        for row in recent_rows
    ]

    return {
        "window_days": days,
        "drift_alert_count": counts.get("template_drift_alert", 0),
        "discovery_cluster_count": counts.get("template_discovery_cluster", 0),
        "recent_events": recent_events,
    }
