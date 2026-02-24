"""
Production pipeline metrics collection.

Tracks key pipeline events in a structured format suitable for
monitoring dashboards, alerting, and accuracy analysis.

Metrics are collected per-job and can be:
- Written to structured JSON logs (for ELK/CloudWatch)
- Stored in job metadata for historical analysis
- Exposed via a metrics endpoint (future: Prometheus)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class StepTiming:
    """Timing for a single pipeline step."""

    step_name: str
    duration_ms: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineMetrics:
    """Complete metrics for a single pipeline run."""

    fax_job_id: str
    tenant_id: str
    total_pages: int = 0
    content_pages: int = 0

    # Timing
    step_timings: list[StepTiming] = field(default_factory=list)
    total_duration_ms: float = 0.0

    # OCR metrics
    avg_ocr_confidence: float = 0.0
    avg_blur_score: float = 0.0
    total_tokens: int = 0

    # Detection metrics
    payer_detected: str = "UNKNOWN"
    payer_confidence: float = 0.0
    doc_type_detected: str = "UNKNOWN"
    doc_type_confidence: float = 0.0

    # Template matching metrics
    template_matched: bool = False
    template_name: str | None = None
    template_match_score: float = 0.0
    template_match_page: int | None = None
    template_match_mode: str = "none"  # strict, adaptive, none
    template_verified: bool = False

    # Document splitting
    is_composite: bool = False
    segment_count: int = 1
    active_segment_pages: int = 0

    # Extraction metrics
    template_fields_extracted: int = 0
    ocr_label_fields_extracted: int = 0
    total_fields_extracted: int = 0
    fields_validated: int = 0
    fields_validation_failed: int = 0

    # Scoring
    overall_confidence: float = 0.0
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    ocr_quality_penalty: float = 0.0

    # Outcome
    final_status: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "fax_job_id": self.fax_job_id,
            "tenant_id": self.tenant_id,
            "total_pages": self.total_pages,
            "content_pages": self.content_pages,
            "total_duration_ms": round(self.total_duration_ms, 1),
            "step_timings": [
                {
                    "step": t.step_name,
                    "ms": round(t.duration_ms, 1),
                    **t.details,
                }
                for t in self.step_timings
            ],
            "ocr": {
                "avg_confidence": round(self.avg_ocr_confidence, 4),
                "avg_blur_score": round(self.avg_blur_score, 1),
                "total_tokens": self.total_tokens,
            },
            "detection": {
                "payer": self.payer_detected,
                "payer_confidence": round(self.payer_confidence, 4),
                "doc_type": self.doc_type_detected,
                "doc_type_confidence": round(self.doc_type_confidence, 4),
            },
            "template": {
                "matched": self.template_matched,
                "name": self.template_name,
                "score": round(self.template_match_score, 4),
                "page": self.template_match_page,
                "mode": self.template_match_mode,
                "verified": self.template_verified,
            },
            "splitting": {
                "is_composite": self.is_composite,
                "segment_count": self.segment_count,
                "active_segment_pages": self.active_segment_pages,
            },
            "extraction": {
                "template_fields": self.template_fields_extracted,
                "ocr_label_fields": self.ocr_label_fields_extracted,
                "total_fields": self.total_fields_extracted,
                "validated": self.fields_validated,
                "validation_failed": self.fields_validation_failed,
            },
            "scoring": {
                "overall_confidence": round(self.overall_confidence, 4),
                "ocr_quality_penalty": round(self.ocr_quality_penalty, 4),
                "needs_review": self.needs_review,
                "review_reasons": self.review_reasons,
            },
            "outcome": self.final_status,
        }

    def log_summary(self) -> None:
        """Log a structured summary of the pipeline run."""
        logger.info(
            "pipeline_metrics | job=%s | pages=%d/%d | payer=%s(%.2f) | "
            "doc=%s(%.2f) | template=%s(%.3f,p%s,%s) | "
            "fields=%d(tmpl=%d,label=%d) | conf=%.3f | "
            "review=%s(%s) | status=%s | duration=%.0fms",
            self.fax_job_id[:8],
            self.content_pages,
            self.total_pages,
            self.payer_detected,
            self.payer_confidence,
            self.doc_type_detected,
            self.doc_type_confidence,
            self.template_name or "none",
            self.template_match_score,
            self.template_match_page,
            self.template_match_mode,
            self.total_fields_extracted,
            self.template_fields_extracted,
            self.ocr_label_fields_extracted,
            self.overall_confidence,
            self.needs_review,
            ",".join(self.review_reasons[:3]) if self.review_reasons else "none",
            self.final_status,
            self.total_duration_ms,
        )


class StepTimer:
    """Context manager for timing pipeline steps."""

    def __init__(self, metrics: PipelineMetrics, step_name: str):
        self.metrics = metrics
        self.step_name = step_name
        self.start_time = 0.0
        self.details: dict[str, Any] = {}

    def __enter__(self) -> "StepTimer":
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        elapsed_ms = (time.perf_counter() - self.start_time) * 1000
        self.metrics.step_timings.append(
            StepTiming(
                step_name=self.step_name,
                duration_ms=elapsed_ms,
                details=self.details,
            )
        )
        return None

    def add_detail(self, key: str, value: Any) -> None:
        """Add a detail to this step's metrics."""
        self.details[key] = value
