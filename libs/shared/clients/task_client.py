"""
TaskClient stub.

Creates review tasks in an external task management / workflow system.
This is a stub implementation — replace with actual integration when
the external task service is available.
"""

import logging
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


class TaskClient:
    """Stub client for external task/workflow management."""

    def create_review_task(
        self,
        fax_job_id: str,
        reason_codes: list[str],
    ) -> dict[str, Any]:
        """Create a human-review task for a fax job.

        Args:
            fax_job_id: UUID of the fax job requiring review.
            reason_codes: List of reason codes explaining why review is needed.

        Returns:
            Acknowledgement dict with task_id.
        """
        task_id = str(uuid4())
        logger.info(
            "TaskClient.create_review_task called (stub) — fax_job_id=%s, "
            "reasons=%s, task_id=%s",
            fax_job_id,
            reason_codes,
            task_id,
        )
        return {"status": "created", "task_id": task_id}
