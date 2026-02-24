"""
Quality analytics and feedback endpoints.

Provides dashboard metrics, per-payer stats, feedback summaries,
and confidence recalibration triggers.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from libs.shared.db.repositories.analytics_repo import AnalyticsRepository
from libs.shared.db.session import get_db
from libs.shared.security.auth import AuthUser, get_current_user

router = APIRouter()


@router.get("/quality")
def get_quality_metrics(
    days: int = Query(default=30, ge=1, le=365, description="Lookback period in days"),
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Get overall quality metrics.

    Returns processing counts, auto-finalize rate, average confidence,
    average processing time, and doc type distribution.
    """
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
    repo = AnalyticsRepository(db)
    stats = repo.get_payer_stats(payer_name=payer_name, days=days)
    corrections = repo.get_corrections_by_payer(payer_name=payer_name, days=days)

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
    repo = AnalyticsRepository(db)
    return repo.get_payer_stats(days=days)


@router.get("/feedback-summary")
def get_feedback_summary(
    days: int = Query(default=30, ge=1, le=365),
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Get feedback and correction summary with most-corrected fields."""
    repo = AnalyticsRepository(db)
    summary = repo.get_feedback_summary(days=days)
    common_corrections = repo.get_common_corrections(days=days)

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
    Trigger confidence threshold recalibration based on feedback data.

    Analyzes correction history and recommends per-payer,
    per-field confidence threshold adjustments.
    """
    from libs.shared.feedback.analyzer import FeedbackAnalyzer

    analyzer = FeedbackAnalyzer(db)
    recommendations = analyzer.analyze_and_recommend(days=days)

    return {
        "status": "completed",
        "period_days": days,
        "recommendations": recommendations,
    }
