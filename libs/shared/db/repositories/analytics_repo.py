"""
Analytics repository for quality metrics and dashboard data.

Provides aggregate queries for processing stats, accuracy by payer,
common errors, and feedback patterns.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from libs.shared.db.models.enums import FaxJobStatusEnum, PayerNameEnum
from libs.shared.db.models.fax_job import FaxJob
from libs.shared.db.models.fax_review import FaxFeedback, FaxReview


class AnalyticsRepository:
    """Repository for aggregate analytics queries."""

    def __init__(self, db: Session):
        self.db = db

    def get_quality_overview(self, days: int = 30) -> dict[str, Any]:
        """Get overall quality metrics for the last N days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        stmt = (
            select(FaxJob.status, func.count().label("cnt"))
            .where(FaxJob.created_at >= cutoff)
            .group_by(FaxJob.status)
        )
        rows = self.db.execute(stmt).all()
        status_counts: dict[str, int] = {r.status.value: r.cnt for r in rows}

        total = sum(status_counts.values())
        completed = status_counts.get(FaxJobStatusEnum.COMPLETED.value, 0)
        needs_review = status_counts.get(FaxJobStatusEnum.NEEDS_REVIEW.value, 0)
        failed = status_counts.get(FaxJobStatusEnum.FAILED.value, 0)

        avg_conf = self.db.execute(
            select(func.avg(FaxJob.overall_conf)).where(
                FaxJob.created_at >= cutoff, FaxJob.overall_conf.isnot(None)
            )
        ).scalar() or 0.0

        avg_proc_time = self.db.execute(
            select(
                func.avg(
                    func.extract(
                        "epoch",
                        FaxJob.processing_completed_at - FaxJob.processing_started_at,
                    )
                )
            ).where(
                FaxJob.created_at >= cutoff,
                FaxJob.processing_started_at.isnot(None),
                FaxJob.processing_completed_at.isnot(None),
            )
        ).scalar() or 0.0

        processed = completed + needs_review
        return {
            "period_days": days,
            "total_jobs": total,
            "total_processed": processed,
            "auto_finalized": completed,
            "needs_review": needs_review,
            "failed": failed,
            "avg_confidence": round(float(avg_conf), 4),
            "auto_finalize_rate": round(completed / processed, 4) if processed else 0.0,
            "avg_processing_seconds": round(float(avg_proc_time), 1),
        }

    def get_payer_stats(
        self, payer_name: str | None = None, days: int = 30
    ) -> list[dict[str, Any]]:
        """Get per-payer processing stats."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        conditions = [FaxJob.created_at >= cutoff]
        if payer_name:
            try:
                conditions.append(FaxJob.payer_hint == PayerNameEnum(payer_name.upper()))
            except ValueError:
                return []

        stmt = (
            select(
                FaxJob.payer_hint,
                func.count().label("total"),
                func.sum(case((FaxJob.status == FaxJobStatusEnum.COMPLETED, 1), else_=0)).label("auto_finalized"),
                func.sum(case((FaxJob.status == FaxJobStatusEnum.NEEDS_REVIEW, 1), else_=0)).label("needs_review"),
                func.avg(FaxJob.overall_conf).label("avg_confidence"),
                func.avg(
                    func.extract("epoch", FaxJob.processing_completed_at - FaxJob.processing_started_at)
                ).label("avg_processing_seconds"),
            )
            .where(*conditions)
            .group_by(FaxJob.payer_hint)
            .order_by(func.count().desc())
        )
        results = []
        for row in self.db.execute(stmt).all():
            auto = row.auto_finalized or 0
            rev = row.needs_review or 0
            processed = auto + rev
            results.append({
                "payer": row.payer_hint.value if row.payer_hint else "UNKNOWN",
                "total": row.total or 0,
                "auto_finalized": auto,
                "needs_review": rev,
                "auto_finalize_rate": round(auto / processed, 4) if processed else 0.0,
                "avg_confidence": round(float(row.avg_confidence or 0), 4),
                "avg_processing_seconds": round(float(row.avg_processing_seconds or 0), 1),
            })
        return results

    def get_common_corrections(self, days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
        """Get most frequently corrected fields from feedback."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(FaxFeedback.field_key, func.count().label("correction_count"))
            .where(FaxFeedback.created_at >= cutoff, FaxFeedback.feedback_type == "correction")
            .group_by(FaxFeedback.field_key)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [{"field_key": r.field_key, "correction_count": r.correction_count} for r in self.db.execute(stmt).all()]

    def get_corrections_by_payer(self, payer_name: str, days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
        """Get most corrected fields for a specific payer."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        try:
            payer_enum = PayerNameEnum(payer_name.upper())
        except ValueError:
            return []
        stmt = (
            select(FaxFeedback.field_key, func.count().label("correction_count"))
            .join(FaxJob, FaxJob.fax_job_id == FaxFeedback.fax_job_id)
            .where(FaxFeedback.created_at >= cutoff, FaxFeedback.feedback_type == "correction", FaxJob.payer_hint == payer_enum)
            .group_by(FaxFeedback.field_key)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [{"field_key": r.field_key, "correction_count": r.correction_count} for r in self.db.execute(stmt).all()]

    def get_feedback_summary(self, days: int = 30) -> dict[str, Any]:
        """Get summary statistics about feedback/corrections."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        total_feedbacks = self.db.execute(
            select(func.count()).select_from(FaxFeedback).where(FaxFeedback.created_at >= cutoff)
        ).scalar() or 0

        total_corrections = self.db.execute(
            select(func.count()).select_from(FaxFeedback).where(
                FaxFeedback.created_at >= cutoff, FaxFeedback.feedback_type == "correction"
            )
        ).scalar() or 0

        reviews_submitted = self.db.execute(
            select(func.count()).select_from(FaxReview).where(
                FaxReview.created_at >= cutoff, FaxReview.submitted_at.isnot(None)
            )
        ).scalar() or 0

        avg_review_time = self.db.execute(
            select(func.avg(FaxReview.time_to_submit_seconds)).where(
                FaxReview.created_at >= cutoff, FaxReview.time_to_submit_seconds.isnot(None)
            )
        ).scalar() or 0.0

        reviews_with_corrections = self.db.execute(
            select(func.count(func.distinct(FaxFeedback.review_id))).where(
                FaxFeedback.created_at >= cutoff,
                FaxFeedback.feedback_type == "correction",
                FaxFeedback.review_id.isnot(None),
            )
        ).scalar() or 0

        correction_rate = reviews_with_corrections / reviews_submitted if reviews_submitted else 0.0

        return {
            "period_days": days,
            "total_feedbacks": total_feedbacks,
            "total_corrections": total_corrections,
            "reviews_submitted": reviews_submitted,
            "reviews_with_corrections": reviews_with_corrections,
            "correction_rate": round(correction_rate, 4),
            "avg_review_seconds": round(float(avg_review_time), 1),
        }

    def get_processing_time_stats(self, days: int = 30) -> dict[str, Any]:
        """Get processing time statistics (avg, p50, p95)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        duration_expr = func.extract("epoch", FaxJob.processing_completed_at - FaxJob.processing_started_at)
        stmt = (
            select(
                func.avg(duration_expr).label("avg"),
                func.percentile_cont(0.5).within_group(duration_expr).label("p50"),
                func.percentile_cont(0.95).within_group(duration_expr).label("p95"),
            ).where(
                FaxJob.created_at >= cutoff,
                FaxJob.processing_started_at.isnot(None),
                FaxJob.processing_completed_at.isnot(None),
            )
        )
        row = self.db.execute(stmt).one_or_none()
        if not row or row.avg is None:
            return {"avg_seconds": 0.0, "p50_seconds": 0.0, "p95_seconds": 0.0}
        return {
            "avg_seconds": round(float(row.avg), 1),
            "p50_seconds": round(float(row.p50), 1),
            "p95_seconds": round(float(row.p95), 1),
        }

    def get_doc_type_distribution(self, days: int = 30) -> list[dict[str, Any]]:
        """Get distribution of document types."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(FaxJob.doc_type, func.count().label("count"))
            .where(FaxJob.created_at >= cutoff)
            .group_by(FaxJob.doc_type)
            .order_by(func.count().desc())
        )
        return [
            {"doc_type": r.doc_type.value if r.doc_type else "UNKNOWN", "count": r.count}
            for r in self.db.execute(stmt).all()
        ]
