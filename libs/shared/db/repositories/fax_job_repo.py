"""
FaxJob repository with specialized queries.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from libs.shared.db.models.enums import FaxJobStatusEnum, PayerNameEnum
from libs.shared.db.models.fax_job import FaxJob
from libs.shared.db.models.fax_page import FaxPage
from libs.shared.db.repositories.base import BaseRepository


class FaxJobRepository(BaseRepository[FaxJob]):
    """
    Repository for FaxJob model with specialized queries.
    """

    def __init__(self, db: Session):
        super().__init__(db, FaxJob)

    def get_by_id_with_pages(self, fax_job_id: UUID) -> FaxJob | None:
        """
        Get fax job with pages eagerly loaded.

        Args:
            fax_job_id: Fax job UUID.

        Returns:
            FaxJob with pages or None.
        """
        stmt = (
            select(FaxJob)
            .options(joinedload(FaxJob.pages))
            .where(FaxJob.fax_job_id == fax_job_id)
        )
        return self.db.execute(stmt).unique().scalar_one_or_none()

    def get_by_id_with_ocr(self, fax_job_id: UUID) -> FaxJob | None:
        """
        Get fax job with pages and OCR tokens eagerly loaded.

        Args:
            fax_job_id: Fax job UUID.

        Returns:
            FaxJob with pages and ocr_tokens or None.
        """
        stmt = (
            select(FaxJob)
            .options(
                selectinload(FaxJob.pages).selectinload(FaxPage.ocr_tokens)
            )
            .where(FaxJob.fax_job_id == fax_job_id)
        )
        return self.db.execute(stmt).unique().scalar_one_or_none()

    def get_by_tenant(
        self,
        tenant_id: str | None,
        status: FaxJobStatusEnum | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[FaxJob]:
        """
        Get fax jobs with optional tenant and status filters.

        Args:
            tenant_id: Tenant identifier. None = all tenants (admin only).
            status: Optional status filter.
            skip: Records to skip.
            limit: Maximum records to return.

        Returns:
            List of FaxJob instances.
        """
        stmt = (
            select(FaxJob)
            .order_by(FaxJob.created_at.desc())
            .offset(skip)
            .limit(limit)
        )

        if tenant_id is not None:
            stmt = stmt.where(FaxJob.tenant_id == tenant_id)

        if status:
            stmt = stmt.where(FaxJob.status == status)

        return list(self.db.execute(stmt).scalars().all())

    def get_by_sha256(self, file_sha256: str, tenant_id: str | None = None) -> FaxJob | None:
        """
        Find fax job by file hash (for deduplication).

        Args:
            file_sha256: SHA-256 hash of the file.
            tenant_id: Restrict search to this tenant (HIPAA: prevent cross-tenant dedup).

        Returns:
            Existing FaxJob or None.
        """
        stmt = select(FaxJob).where(FaxJob.file_sha256 == file_sha256)
        if tenant_id is not None:
            stmt = stmt.where(FaxJob.tenant_id == tenant_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_pending_jobs(self, limit: int = 10) -> list[FaxJob]:
        """
        Atomically fetch pending jobs and mark them PROCESSING.

        Uses SELECT ... FOR UPDATE SKIP LOCKED so concurrent workers
        cannot grab the same job.  The status is set to PROCESSING
        within the same lock window (before flush releases the row lock).

        Args:
            limit: Maximum jobs to return.

        Returns:
            List of FaxJob instances now marked PROCESSING.
        """
        stmt = (
            select(FaxJob)
            .where(FaxJob.status == FaxJobStatusEnum.PENDING)
            .order_by(FaxJob.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        jobs = list(self.db.execute(stmt).scalars().all())

        # Mark as PROCESSING while rows are still locked
        for job in jobs:
            job.status = FaxJobStatusEnum.PROCESSING
            job.processing_started_at = datetime.now(timezone.utc)

        if jobs:
            self.db.flush()

        return jobs

    def get_jobs_needing_review(
        self,
        tenant_id: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[FaxJob]:
        """
        Get jobs that need human review.

        Args:
            tenant_id: Optional tenant filter.
            skip: Records to skip.
            limit: Maximum records to return.

        Returns:
            List of FaxJob instances needing review.
        """
        conditions = [FaxJob.needs_review == True]

        if tenant_id:
            conditions.append(FaxJob.tenant_id == tenant_id)

        stmt = (
            select(FaxJob)
            .where(and_(*conditions))
            .order_by(FaxJob.created_at.asc())
            .offset(skip)
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_jobs_by_template(
        self,
        template_version_id: UUID,
        skip: int = 0,
        limit: int = 100,
    ) -> list[FaxJob]:
        """
        Get jobs matched to a specific template version.

        Args:
            template_version_id: Template version UUID.
            skip: Records to skip.
            limit: Maximum records to return.

        Returns:
            List of FaxJob instances.
        """
        stmt = (
            select(FaxJob)
            .where(FaxJob.matched_template_version_id == template_version_id)
            .order_by(FaxJob.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())

    def update_status(
        self,
        fax_job_id: UUID,
        status: FaxJobStatusEnum,
    ) -> FaxJob | None:
        """
        Update job status.

        Args:
            fax_job_id: Fax job UUID.
            status: New status.

        Returns:
            Updated FaxJob or None.
        """
        job = self.get_by_id(fax_job_id)
        if not job:
            return None

        job.status = status

        if status == FaxJobStatusEnum.PROCESSING:
            job.processing_started_at = datetime.now(timezone.utc)
        elif status == FaxJobStatusEnum.NEEDS_REVIEW:
            job.needs_review = True
        elif status in (FaxJobStatusEnum.COMPLETED, FaxJobStatusEnum.FAILED):
            if job.processing_completed_at is None:
                job.processing_completed_at = datetime.now(timezone.utc)
            if status == FaxJobStatusEnum.COMPLETED:
                job.needs_review = False

        self.db.flush()
        return job

    def mark_processing_started(self, fax_job_id: UUID) -> FaxJob | None:
        """Mark job as processing."""
        return self.update_status(fax_job_id, FaxJobStatusEnum.PROCESSING)

    def mark_completed(
        self,
        fax_job_id: UUID,
        overall_conf: float,
        needs_review: bool = False,
    ) -> FaxJob | None:
        """
        Mark job as completed.

        Args:
            fax_job_id: Fax job UUID.
            overall_conf: Overall confidence score.
            needs_review: Whether human review is needed.

        Returns:
            Updated FaxJob or None.
        """
        job = self.get_by_id(fax_job_id)
        if not job:
            return None

        job.status = FaxJobStatusEnum.NEEDS_REVIEW if needs_review else FaxJobStatusEnum.COMPLETED
        job.overall_conf = overall_conf
        job.needs_review = needs_review
        job.processing_completed_at = datetime.now(timezone.utc)

        self.db.flush()
        return job

    def mark_failed(self, fax_job_id: UUID, error: str | None = None) -> FaxJob | None:
        """
        Mark job as failed.

        Args:
            fax_job_id: Fax job UUID.
            error: Optional error message.

        Returns:
            Updated FaxJob or None.
        """
        job = self.get_by_id(fax_job_id)
        if not job:
            return None

        job.status = FaxJobStatusEnum.FAILED
        job.processing_completed_at = datetime.now(timezone.utc)
        if error:
            job.job_metadata = {**(job.job_metadata or {}), "error": error}

        self.db.flush()
        return job

    def set_template_match(
        self,
        fax_job_id: UUID,
        template_version_id: UUID,
        match_score: float,
    ) -> FaxJob | None:
        """
        Set template match result.

        Args:
            fax_job_id: Fax job UUID.
            template_version_id: Matched template version UUID.
            match_score: Match confidence score.

        Returns:
            Updated FaxJob or None.
        """
        job = self.get_by_id(fax_job_id)
        if not job:
            return None

        job.matched_template_version_id = template_version_id
        job.matched_template_score = match_score

        self.db.flush()
        return job

    def count_by_tenant(
        self,
        tenant_id: str | None,
        status: FaxJobStatusEnum | None = None,
    ) -> int:
        """Count jobs for a tenant with optional status filter. None tenant = all tenants."""
        stmt = select(func.count()).select_from(FaxJob)
        if tenant_id is not None:
            stmt = stmt.where(FaxJob.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(FaxJob.status == status)
        return self.db.execute(stmt).scalar() or 0

    def count_by_status(self, tenant_id: str) -> dict[str, int]:
        """
        Get job counts grouped by status for a tenant.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            Dictionary of status -> count.
        """
        stmt = (
            select(FaxJob.status, func.count())
            .where(FaxJob.tenant_id == tenant_id)
            .group_by(FaxJob.status)
        )
        results = self.db.execute(stmt).all()
        return {status.value: count for status, count in results}
