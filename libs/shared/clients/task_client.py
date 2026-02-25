"""
Task workflow integration client.

Persists a concrete review-workflow task payload on the review packet and
job metadata so downstream systems (UI, analytics, exports) can consume a
stable task object without any placeholder behavior.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from libs.shared.db.models.audit_log import AuditAction, AuditLog
from libs.shared.db.repositories.fax_job_repo import FaxJobRepository
from libs.shared.db.repositories.review_repo import ReviewRepository

logger = logging.getLogger(__name__)


class TaskClient:
    """Create concrete human-review workflow tasks in the local data store."""

    def __init__(
        self,
        db: Session,
        tenant_id: str | None = None,
        actor: str = "pipeline",
    ) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.actor = actor

    @staticmethod
    def _normalize_reason_codes(reason_codes: list[str] | None) -> list[str]:
        """Normalize reason-code list while preserving order."""
        if not reason_codes:
            return []

        normalized: list[str] = []
        seen: set[str] = set()
        for raw in reason_codes:
            code = (raw or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            normalized.append(code)
        return normalized[:16]

    def create_review_task(
        self,
        fax_job_id: str,
        reason_codes: list[str],
    ) -> dict[str, Any]:
        """Create or return an idempotent review task for a fax job."""
        try:
            job_uuid = UUID(fax_job_id)
        except ValueError as exc:
            raise ValueError(f"Invalid fax_job_id: {fax_job_id}") from exc

        job_repo = FaxJobRepository(self.db)
        review_repo = ReviewRepository(self.db)

        job = job_repo.get_by_id(job_uuid)
        if not job:
            raise ValueError(f"Fax job not found for review task: {fax_job_id}")

        if self.tenant_id and job.tenant_id != self.tenant_id:
            raise ValueError(
                f"Tenant mismatch for review task: expected {self.tenant_id}, got {job.tenant_id}"
            )

        review = review_repo.get_by_job(job_uuid)
        if not review:
            raise ValueError(f"Review record not found for fax job: {fax_job_id}")

        packet = dict(review.review_packet or {})
        existing_task = packet.get("workflow_task")
        if isinstance(existing_task, dict) and existing_task.get("task_id"):
            return {
                "status": "existing",
                "task_id": str(existing_task["task_id"]),
            }

        normalized_reasons = self._normalize_reason_codes(reason_codes)
        task_id = f"review-{review.review_id}"
        created_at = datetime.now(timezone.utc).isoformat()

        workflow_task = {
            "task_id": task_id,
            "task_type": "HUMAN_REVIEW",
            "status": "OPEN",
            "fax_job_id": str(job.fax_job_id),
            "review_id": str(review.review_id),
            "reason_codes": normalized_reasons,
            "created_at": created_at,
            "created_by": self.actor,
        }

        packet["workflow_task"] = workflow_task
        review.review_packet = packet

        meta = dict(job.job_metadata or {})
        meta["workflow_task"] = workflow_task
        job.job_metadata = meta

        self.db.add(
            AuditLog.log_action(
                tenant_id=job.tenant_id,
                user_id=self.actor,
                action=AuditAction.PROCESS,
                resource_type="workflow_task",
                resource_id=review.review_id,
                details={
                    "fax_job_id": str(job.fax_job_id),
                    "task_id": task_id,
                    "reason_codes": normalized_reasons,
                },
            )
        )

        self.db.flush()

        logger.info(
            "Created workflow task %s for fax job %s (reasons=%d)",
            task_id,
            str(job.fax_job_id)[:8],
            len(normalized_reasons),
        )

        return {"status": "created", "task_id": task_id}
