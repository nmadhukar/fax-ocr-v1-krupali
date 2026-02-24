"""
Feedback analyzer for confidence recalibration and quality insights.

Analyzes correction patterns from human review to:
- Identify most-corrected fields per payer
- Detect systematic OCR errors
- Produce confidence calibration recommendations
- Flag potential template ROI drift
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from libs.shared.db.models.fax_job import FaxJob
from libs.shared.db.models.fax_review import FaxFeedback


logger = logging.getLogger(__name__)


class FeedbackAnalyzer:
    """Analyzes feedback data to produce actionable recalibration recommendations."""

    HIGH_CORRECTION_RATE = 0.30
    MIN_SAMPLES = 5

    def __init__(self, db: Session):
        self.db = db

    def analyze_and_recommend(self, days: int = 30) -> dict[str, Any]:
        """Analyze feedback data and produce recalibration recommendations."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        corrections = self._get_corrections(cutoff)

        if not corrections:
            return {
                "message": "No correction data available for analysis",
                "payer_recommendations": [],
                "field_recommendations": [],
                "ocr_error_patterns": [],
                "roi_drift_warnings": [],
            }

        return {
            "total_corrections_analyzed": len(corrections),
            "field_recommendations": self._analyze_field_corrections(corrections),
            "payer_recommendations": self._analyze_payer_corrections(corrections, cutoff),
            "ocr_error_patterns": self._detect_ocr_patterns(corrections),
            "roi_drift_warnings": self._check_roi_drift(corrections),
        }

    def _get_corrections(self, cutoff: datetime) -> list[dict[str, Any]]:
        """Fetch all correction feedback records with job metadata."""
        stmt = (
            select(
                FaxFeedback.field_key,
                FaxFeedback.original_value,
                FaxFeedback.corrected_value,
                FaxJob.payer_hint,
                FaxJob.doc_type,
            )
            .join(FaxJob, FaxJob.fax_job_id == FaxFeedback.fax_job_id)
            .where(
                FaxFeedback.created_at >= cutoff,
                FaxFeedback.feedback_type == "correction",
            )
        )
        return [
            {
                "field_key": r.field_key,
                "original_value": r.original_value,
                "corrected_value": r.corrected_value,
                "payer": r.payer_hint.value if r.payer_hint else "UNKNOWN",
                "doc_type": r.doc_type.value if r.doc_type else "UNKNOWN",
            }
            for r in self.db.execute(stmt).all()
        ]

    def _analyze_field_corrections(self, corrections: list[dict]) -> list[dict[str, Any]]:
        """Analyze correction rates per field and recommend threshold changes."""
        field_counts: dict[str, int] = defaultdict(int)
        for c in corrections:
            field_counts[c["field_key"]] += 1

        total_jobs = self.db.execute(select(func.count()).select_from(FaxJob)).scalar() or 1

        recommendations = []
        for field_key, count in sorted(field_counts.items(), key=lambda x: -x[1]):
            rate = count / total_jobs
            if count >= self.MIN_SAMPLES and rate > self.HIGH_CORRECTION_RATE:
                recommendations.append({
                    "field_key": field_key,
                    "correction_count": count,
                    "correction_rate": round(rate, 4),
                    "recommendation": "LOWER_AUTO_FINALIZE_THRESHOLD",
                    "detail": (
                        f"Field '{field_key}' is corrected {rate:.0%} of the time. "
                        f"Consider lowering the confidence threshold or flagging for review."
                    ),
                })
            elif count >= self.MIN_SAMPLES:
                recommendations.append({
                    "field_key": field_key,
                    "correction_count": count,
                    "correction_rate": round(rate, 4),
                    "recommendation": "MONITOR",
                    "detail": f"Field '{field_key}' has moderate correction rate.",
                })
        return recommendations

    def _analyze_payer_corrections(
        self, corrections: list[dict], cutoff: datetime
    ) -> list[dict[str, Any]]:
        """Analyze correction patterns per payer."""
        payer_corrections: dict[str, int] = defaultdict(int)
        for c in corrections:
            payer_corrections[c["payer"]] += 1

        stmt = (
            select(FaxJob.payer_hint, func.count().label("total"))
            .where(FaxJob.created_at >= cutoff)
            .group_by(FaxJob.payer_hint)
        )
        payer_totals = {
            r.payer_hint.value if r.payer_hint else "UNKNOWN": r.total
            for r in self.db.execute(stmt).all()
        }

        recommendations = []
        for payer, corr_count in sorted(payer_corrections.items(), key=lambda x: -x[1]):
            total = payer_totals.get(payer, 1)
            rate = corr_count / total
            action = "OK"
            if rate > self.HIGH_CORRECTION_RATE:
                action = "INCREASE_REVIEW_THRESHOLD"
            elif rate > 0.15:
                action = "MONITOR"
            recommendations.append({
                "payer": payer,
                "total_jobs": total,
                "correction_count": corr_count,
                "correction_rate": round(rate, 4),
                "recommendation": action,
            })
        return recommendations

    def _detect_ocr_patterns(self, corrections: list[dict]) -> list[dict[str, Any]]:
        """Detect systematic OCR misread patterns (0/O, 1/I/l, 5/S, etc.)."""
        substitutions: dict[tuple[str, str], int] = defaultdict(int)
        for c in corrections:
            orig = c.get("original_value") or ""
            corr = c.get("corrected_value") or ""
            if len(orig) == len(corr) and orig != corr:
                for o_char, c_char in zip(orig, corr):
                    if o_char != c_char:
                        substitutions[(o_char, c_char)] += 1

        return [
            {"ocr_reads": o, "should_be": c, "occurrences": n}
            for (o, c), n in sorted(substitutions.items(), key=lambda x: -x[1])
            if n >= 3
        ][:10]

    def _check_roi_drift(self, corrections: list[dict]) -> list[dict[str, Any]]:
        """Flag fields with high correction counts (potential template ROI drift)."""
        field_counts: dict[str, int] = defaultdict(int)
        for c in corrections:
            field_counts[c["field_key"]] += 1
        return [
            {
                "field_key": fk,
                "recent_corrections": cnt,
                "warning": f"Field '{fk}' has {cnt} corrections — check if template ROI has shifted.",
            }
            for fk, cnt in field_counts.items()
            if cnt >= 5
        ]
