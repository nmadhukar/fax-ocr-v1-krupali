"""
Prior authorization integration client.

Stores prior-auth case lifecycle and extraction attachment metadata in the
job record so downstream systems can inspect finalized case artifacts.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from libs.shared.db.models.audit_log import AuditAction, AuditLog
from libs.shared.db.models.fax_job import FaxJob
from libs.shared.db.repositories.extraction_repo import ExtractionRepository
from libs.shared.db.repositories.fax_job_repo import FaxJobRepository

logger = logging.getLogger(__name__)


class PriorAuthClient:
    """Manage prior-auth case metadata for completed fax jobs."""

    def __init__(
        self,
        db: Session,
        tenant_id: str | None = None,
        actor: str = "pipeline",
    ) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.actor = actor

    def _resolve_job(self, fax_job_id: str | None, case_id: str | None) -> FaxJob | None:
        """Resolve the target fax job from fax_job_id or case identifier."""
        repo = FaxJobRepository(self.db)

        if fax_job_id:
            try:
                return repo.get_by_id(UUID(fax_job_id))
            except ValueError as exc:
                raise ValueError(f"Invalid fax_job_id: {fax_job_id}") from exc

        if not case_id:
            return None

        try:
            job_uuid = UUID(case_id)
            job = repo.get_by_id(job_uuid)
            if job:
                return job
        except ValueError:
            pass

        stmt = select(FaxJob).where(
            or_(
                FaxJob.external_fax_id == case_id,
                FaxJob.file_sha256 == case_id,
            )
        )
        if self.tenant_id:
            stmt = stmt.where(FaxJob.tenant_id == self.tenant_id)
        stmt = stmt.order_by(FaxJob.created_at.desc())
        return self.db.execute(stmt).scalars().first()

    def _assert_tenant(self, job: FaxJob) -> None:
        """Ensure tenant isolation for integration updates."""
        if self.tenant_id and job.tenant_id != self.tenant_id:
            raise ValueError(
                f"Tenant mismatch for prior-auth integration: expected {self.tenant_id}, got {job.tenant_id}"
            )

    def create_case(
        self,
        patient_name: str | None = None,
        member_id: str | None = None,
        payer: str | None = None,
        fax_job_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a concrete prior-auth case record reference."""
        case_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        job = self._resolve_job(fax_job_id=fax_job_id, case_id=None)

        if job:
            self._assert_tenant(job)
            meta = dict(job.job_metadata or {})
            meta["prior_auth_case"] = {
                "case_id": case_id,
                "status": "CREATED",
                "patient_name": patient_name,
                "member_id": member_id,
                "payer": payer,
                "created_at": now,
                "created_by": self.actor,
            }
            job.job_metadata = meta

            self.db.add(
                AuditLog.log_action(
                    tenant_id=job.tenant_id,
                    user_id=self.actor,
                    action=AuditAction.CREATE,
                    resource_type="prior_auth_case",
                    resource_id=job.fax_job_id,
                    details={"case_id": case_id},
                )
            )
            self.db.flush()

        logger.info(
            "Created prior-auth case %s (job=%s)",
            case_id,
            str(job.fax_job_id)[:8] if job else "n/a",
        )

        return {"status": "created", "case_id": case_id}

    def attach_extraction(
        self,
        case_id: str,
        extraction_json: dict[str, Any],
        fax_job_id: str | None = None,
    ) -> dict[str, Any]:
        """Attach final extraction payload metadata to a prior-auth case."""
        if not case_id:
            raise ValueError("case_id is required")

        job = self._resolve_job(fax_job_id=fax_job_id, case_id=case_id)
        if not job:
            logger.warning(
                "Prior-auth attach skipped: no job found for case_id=%s, fax_job_id=%s",
                case_id,
                fax_job_id,
            )
            return {
                "status": "not_found",
                "case_id": case_id,
                "attached": False,
            }

        self._assert_tenant(job)

        now = datetime.now(timezone.utc).isoformat()
        field_count = len(extraction_json or {})

        meta = deepcopy(job.job_metadata or {})
        raw_case = meta.get("prior_auth_case")
        existing_case = dict(raw_case) if isinstance(raw_case, dict) else {}
        if not existing_case:
            existing_case = {
                "case_id": case_id,
                "created_at": now,
                "created_by": self.actor,
            }

        existing_case.update(
            {
                "case_id": case_id,
                "status": "ATTACHED",
                "attached_at": now,
                "attached_by": self.actor,
                "field_count": field_count,
                "fax_job_id": str(job.fax_job_id),
            }
        )

        meta["prior_auth_case"] = existing_case
        job.job_metadata = meta

        extraction_repo = ExtractionRepository(self.db)
        extraction = extraction_repo.get_by_job(job.fax_job_id)
        if extraction:
            model_versions = dict(extraction.model_versions or {})
            model_versions["prior_auth"] = {
                "case_id": case_id,
                "attached_at": now,
                "attached_by": self.actor,
                "field_count": field_count,
                "integration": "local-prior-auth-client-v1",
            }
            extraction.model_versions = model_versions

        self.db.add(
            AuditLog.log_action(
                tenant_id=job.tenant_id,
                user_id=self.actor,
                action=AuditAction.PROCESS,
                resource_type="prior_auth_case",
                resource_id=job.fax_job_id,
                details={
                    "case_id": case_id,
                    "attached": True,
                    "field_count": field_count,
                },
            )
        )

        self.db.flush()

        logger.info(
            "Attached extraction to prior-auth case %s for fax job %s (fields=%d)",
            case_id,
            str(job.fax_job_id)[:8],
            field_count,
        )

        return {
            "status": "attached",
            "case_id": case_id,
            "fax_job_id": str(job.fax_job_id),
            "attached": True,
            "field_count": field_count,
        }
