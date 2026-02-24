"""
Analytics repository for quality metrics and dashboard data.

Provides aggregate queries for processing stats, accuracy by payer,
common errors, and feedback patterns.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, exists, func, select
from sqlalchemy.orm import Session

from libs.shared.db.models.enums import FaxJobStatusEnum, PayerNameEnum
from libs.shared.db.models.fax_job import FaxJob
from libs.shared.db.models.fax_review import FaxFeedback, FaxReview


class AnalyticsRepository:
    """Repository for aggregate analytics queries."""

    def __init__(self, db: Session):
        self.db = db

    def get_quality_overview(self, days: int = 30, tenant_id: str | None = None) -> dict[str, Any]:
        """Get overall quality metrics for the last N days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        conditions = [FaxJob.created_at >= cutoff]
        if tenant_id:
            conditions.append(FaxJob.tenant_id == tenant_id)

        stmt = (
            select(FaxJob.status, func.count().label("cnt"))
            .where(*conditions)
            .group_by(FaxJob.status)
        )
        rows = self.db.execute(stmt).all()
        status_counts: dict[str, int] = {r.status.value: r.cnt for r in rows}

        total = sum(status_counts.values())
        completed = status_counts.get(FaxJobStatusEnum.COMPLETED.value, 0)
        needs_review = status_counts.get(FaxJobStatusEnum.NEEDS_REVIEW.value, 0)
        failed = status_counts.get(FaxJobStatusEnum.FAILED.value, 0)
        auto_finalized = self._count_auto_finalized(cutoff=cutoff, tenant_id=tenant_id)
        human_review_completed = max(completed - auto_finalized, 0)

        conf_conditions = [FaxJob.created_at >= cutoff, FaxJob.overall_conf.isnot(None)]
        proc_conditions = [
            FaxJob.created_at >= cutoff,
            FaxJob.processing_started_at.isnot(None),
            FaxJob.processing_completed_at.isnot(None),
        ]
        if tenant_id:
            conf_conditions.append(FaxJob.tenant_id == tenant_id)
            proc_conditions.append(FaxJob.tenant_id == tenant_id)

        avg_conf = self.db.execute(
            select(func.avg(FaxJob.overall_conf)).where(*conf_conditions)
        ).scalar() or 0.0

        avg_proc_time = self.db.execute(
            select(
                func.avg(
                    func.extract(
                        "epoch",
                        FaxJob.processing_completed_at - FaxJob.processing_started_at,
                    )
                )
            ).where(*proc_conditions)
        ).scalar() or 0.0

        processed = auto_finalized + human_review_completed + needs_review
        return {
            "period_days": days,
            "total_jobs": total,
            "total_processed": processed,
            "auto_finalized": auto_finalized,
            "human_review_completed": human_review_completed,
            "needs_review": needs_review,
            "failed": failed,
            "avg_confidence": round(float(avg_conf), 4),
            "auto_finalize_rate": round(auto_finalized / processed, 4) if processed else 0.0,
            "avg_processing_seconds": round(float(avg_proc_time), 1),
        }

    def get_payer_stats(
        self, payer_name: str | None = None, days: int = 30, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get per-payer processing stats."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        conditions = [FaxJob.created_at >= cutoff]
        if tenant_id:
            conditions.append(FaxJob.tenant_id == tenant_id)
        if payer_name:
            try:
                conditions.append(FaxJob.payer_hint == PayerNameEnum(payer_name.upper()))
            except ValueError:
                return []

        stmt = (
            select(
                FaxJob.payer_hint,
                func.count().label("total"),
                func.sum(case(
                    (
                        and_(
                            FaxJob.status == FaxJobStatusEnum.COMPLETED,
                            ~exists(
                                select(1).where(
                                    FaxReview.fax_job_id == FaxJob.fax_job_id,
                                    FaxReview.submitted_at.isnot(None),
                                )
                            ),
                        ),
                        1,
                    ),
                    else_=0,
                )).label("auto_finalized"),
                func.sum(case(
                    (
                        and_(
                            FaxJob.status == FaxJobStatusEnum.COMPLETED,
                            exists(
                                select(1).where(
                                    FaxReview.fax_job_id == FaxJob.fax_job_id,
                                    FaxReview.submitted_at.isnot(None),
                                )
                            ),
                        ),
                        1,
                    ),
                    else_=0,
                )).label("human_review_completed"),
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
            human = row.human_review_completed or 0
            rev = row.needs_review or 0
            processed = auto + human + rev
            results.append({
                "payer": row.payer_hint.value if row.payer_hint else "UNKNOWN",
                "total": row.total or 0,
                "auto_finalized": auto,
                "human_review_completed": human,
                "needs_review": rev,
                "auto_finalize_rate": round(auto / processed, 4) if processed else 0.0,
                "avg_confidence": round(float(row.avg_confidence or 0), 4),
                "avg_processing_seconds": round(float(row.avg_processing_seconds or 0), 1),
            })
        return results

    def _count_auto_finalized(self, cutoff: datetime, tenant_id: str | None = None) -> int:
        """Count jobs completed without a submitted human review."""
        review_submitted_exists = exists(
            select(1).where(
                FaxReview.fax_job_id == FaxJob.fax_job_id,
                FaxReview.submitted_at.isnot(None),
            )
        )
        conditions = [
            FaxJob.created_at >= cutoff,
            FaxJob.status == FaxJobStatusEnum.COMPLETED,
            ~review_submitted_exists,
        ]
        if tenant_id:
            conditions.append(FaxJob.tenant_id == tenant_id)
        return self.db.execute(select(func.count()).where(*conditions)).scalar() or 0

    def get_common_corrections(self, days: int = 30, limit: int = 20, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """Get most frequently corrected fields from feedback."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        conditions = [FaxFeedback.created_at >= cutoff, FaxFeedback.feedback_type == "correction"]
        stmt = select(FaxFeedback.field_key, func.count().label("correction_count"))
        if tenant_id:
            stmt = stmt.join(FaxJob, FaxJob.fax_job_id == FaxFeedback.fax_job_id)
            conditions.append(FaxJob.tenant_id == tenant_id)
        stmt = (
            stmt.where(*conditions)
            .group_by(FaxFeedback.field_key)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [{"field_key": r.field_key, "correction_count": r.correction_count} for r in self.db.execute(stmt).all()]

    def get_corrections_by_payer(self, payer_name: str, days: int = 30, limit: int = 20, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """Get most corrected fields for a specific payer."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        try:
            payer_enum = PayerNameEnum(payer_name.upper())
        except ValueError:
            return []
        conditions = [FaxFeedback.created_at >= cutoff, FaxFeedback.feedback_type == "correction", FaxJob.payer_hint == payer_enum]
        if tenant_id:
            conditions.append(FaxJob.tenant_id == tenant_id)
        stmt = (
            select(FaxFeedback.field_key, func.count().label("correction_count"))
            .join(FaxJob, FaxJob.fax_job_id == FaxFeedback.fax_job_id)
            .where(*conditions)
            .group_by(FaxFeedback.field_key)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [{"field_key": r.field_key, "correction_count": r.correction_count} for r in self.db.execute(stmt).all()]

    def get_feedback_summary(self, days: int = 30, tenant_id: str | None = None) -> dict[str, Any]:
        """Get summary statistics about feedback/corrections."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        fb_conditions = [FaxFeedback.created_at >= cutoff]
        fb_corr_conditions = [FaxFeedback.created_at >= cutoff, FaxFeedback.feedback_type == "correction"]
        review_conditions = [FaxReview.created_at >= cutoff, FaxReview.submitted_at.isnot(None)]
        review_time_conditions = [FaxReview.created_at >= cutoff, FaxReview.time_to_submit_seconds.isnot(None)]
        corr_review_conditions = [
            FaxFeedback.created_at >= cutoff,
            FaxFeedback.feedback_type == "correction",
            FaxFeedback.review_id.isnot(None),
        ]

        if tenant_id:
            fb_conditions.append(FaxFeedback.fax_job_id.in_(
                select(FaxJob.fax_job_id).where(FaxJob.tenant_id == tenant_id)
            ))
            fb_corr_conditions.append(FaxFeedback.fax_job_id.in_(
                select(FaxJob.fax_job_id).where(FaxJob.tenant_id == tenant_id)
            ))
            review_conditions.append(FaxReview.fax_job_id.in_(
                select(FaxJob.fax_job_id).where(FaxJob.tenant_id == tenant_id)
            ))
            review_time_conditions.append(FaxReview.fax_job_id.in_(
                select(FaxJob.fax_job_id).where(FaxJob.tenant_id == tenant_id)
            ))
            corr_review_conditions.append(FaxFeedback.fax_job_id.in_(
                select(FaxJob.fax_job_id).where(FaxJob.tenant_id == tenant_id)
            ))

        total_feedbacks = self.db.execute(
            select(func.count()).select_from(FaxFeedback).where(*fb_conditions)
        ).scalar() or 0

        total_corrections = self.db.execute(
            select(func.count()).select_from(FaxFeedback).where(*fb_corr_conditions)
        ).scalar() or 0

        reviews_submitted = self.db.execute(
            select(func.count()).select_from(FaxReview).where(*review_conditions)
        ).scalar() or 0

        avg_review_time = self.db.execute(
            select(func.avg(FaxReview.time_to_submit_seconds)).where(*review_time_conditions)
        ).scalar() or 0.0

        reviews_with_corrections = self.db.execute(
            select(func.count(func.distinct(FaxFeedback.review_id))).where(*corr_review_conditions)
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

    def get_processing_time_stats(self, days: int = 30, tenant_id: str | None = None) -> dict[str, Any]:
        """Get processing time statistics (avg, p50, p95)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        duration_expr = func.extract("epoch", FaxJob.processing_completed_at - FaxJob.processing_started_at)
        conditions = [
            FaxJob.created_at >= cutoff,
            FaxJob.processing_started_at.isnot(None),
            FaxJob.processing_completed_at.isnot(None),
        ]
        if tenant_id:
            conditions.append(FaxJob.tenant_id == tenant_id)
        stmt = (
            select(
                func.avg(duration_expr).label("avg"),
                func.percentile_cont(0.5).within_group(duration_expr).label("p50"),
                func.percentile_cont(0.95).within_group(duration_expr).label("p95"),
            ).where(*conditions)
        )
        row = self.db.execute(stmt).one_or_none()
        if not row or row.avg is None:
            return {"avg_seconds": 0.0, "p50_seconds": 0.0, "p95_seconds": 0.0}
        return {
            "avg_seconds": round(float(row.avg), 1),
            "p50_seconds": round(float(row.p50), 1),
            "p95_seconds": round(float(row.p95), 1),
        }

    def get_doc_type_distribution(self, days: int = 30, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """Get distribution of document types."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        conditions = [FaxJob.created_at >= cutoff]
        if tenant_id:
            conditions.append(FaxJob.tenant_id == tenant_id)
        stmt = (
            select(FaxJob.doc_type, func.count().label("count"))
            .where(*conditions)
            .group_by(FaxJob.doc_type)
            .order_by(func.count().desc())
        )
        return [
            {"doc_type": r.doc_type.value if r.doc_type else "UNKNOWN", "count": r.count}
            for r in self.db.execute(stmt).all()
        ]
