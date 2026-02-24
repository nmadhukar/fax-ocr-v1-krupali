"""
Review and feedback repositories.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from libs.shared.db.models.fax_job import FaxJob
from libs.shared.db.models.fax_review import FaxFeedback, FaxReview
from libs.shared.db.repositories.base import BaseRepository


class ReviewRepository(BaseRepository[FaxReview]):
    """Repository for FaxReview model."""

    def __init__(self, db: Session):
        super().__init__(db, FaxReview)

    def get_by_job(self, fax_job_id: UUID) -> FaxReview | None:
        """Get review for a job."""
        stmt = select(FaxReview).where(FaxReview.fax_job_id == fax_job_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_pending_reviews(
        self,
        skip: int = 0,
        limit: int = 100,
        tenant_id: str | None = None,
    ) -> list[FaxReview]:
        """Get reviews awaiting action, optionally scoped to a tenant."""
        stmt = (
            select(FaxReview)
            .join(FaxJob, FaxReview.fax_job_id == FaxJob.fax_job_id)
            .where(FaxReview.submitted_at == None)  # noqa: E711
            .order_by(FaxReview.priority.desc(), FaxReview.created_at.asc())
            .offset(skip)
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(FaxJob.tenant_id == tenant_id)
        return list(self.db.execute(stmt).scalars().all())

    def get_unclaimed_reviews(
        self,
        skip: int = 0,
        limit: int = 100,
        tenant_id: str | None = None,
    ) -> list[FaxReview]:
        """Get reviews not yet claimed, optionally scoped to a tenant."""
        now = datetime.now(timezone.utc)
        stmt = (
            select(FaxReview)
            .join(FaxJob, FaxReview.fax_job_id == FaxJob.fax_job_id)
            .where(
                FaxReview.submitted_at == None,  # noqa: E711
                # Either not claimed or claim expired
                (FaxReview.claimed_at == None) | (FaxReview.claim_expires_at < now),  # noqa: E711
            )
            .order_by(FaxReview.priority.desc(), FaxReview.created_at.asc())
            .offset(skip)
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(FaxJob.tenant_id == tenant_id)
        return list(self.db.execute(stmt).scalars().all())

    def get_claimed_by_user(
        self,
        user_id: str,
    ) -> list[FaxReview]:
        """Get reviews claimed by a user."""
        stmt = (
            select(FaxReview)
            .where(
                FaxReview.claimed_by == user_id,
                FaxReview.submitted_at == None,  # noqa: E711
            )
        )
        return list(self.db.execute(stmt).scalars().all())

    def claim_review(
        self,
        review_id: UUID,
        user_id: str,
        expiry_minutes: int = 30,
    ) -> FaxReview | None:
        """
        Claim a review for a user.

        Args:
            review_id: Review UUID.
            user_id: User claiming the review.
            expiry_minutes: Minutes until claim expires.

        Returns:
            Claimed FaxReview or None if claim failed.
        """
        review = self.get_by_id(review_id)
        if not review:
            return None

        if review.claim(user_id, expiry_minutes):
            self.db.flush()
            return review
        return None

    def claim_review_atomic(
        self,
        review_id: UUID,
        reviewer_id: str,
        expected_claimed_by: str | None = None,
        expiry_minutes: int = 30,
    ) -> bool:
        """
        Atomically claim a review using optimistic locking.

        Uses a conditional UPDATE that only succeeds if ``claimed_by``
        still matches the expected value.  Prevents race conditions
        when two reviewers try to claim the same review simultaneously.

        Returns:
            True if the claim succeeded, False if another reviewer claimed it first.
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=expiry_minutes)

        # Build the WHERE condition: review_id matches AND claimed_by
        # is still what we expect (optimistic concurrency check)
        conditions = [
            FaxReview.review_id == review_id,
            FaxReview.submitted_at == None,  # noqa: E711
        ]
        if expected_claimed_by is None:
            conditions.append(
                (FaxReview.claimed_by == None) | (FaxReview.claim_expires_at < now)  # noqa: E711
            )
        else:
            conditions.append(FaxReview.claimed_by == expected_claimed_by)
            # If trying to take over another reviewer's claim, require it to
            # still be expired at update time (prevents stale-claim races).
            if expected_claimed_by != reviewer_id:
                conditions.append(FaxReview.claim_expires_at < now)

        stmt = (
            update(FaxReview)
            .where(*conditions)
            .values(
                claimed_by=reviewer_id,
                claimed_at=now,
                claim_expires_at=expires_at,
            )
        )

        result = self.db.execute(stmt)
        self.db.flush()
        return result.rowcount > 0

    def submit_review(
        self,
        review_id: UUID,
        user_id: str,
        corrections: dict | None = None,
    ) -> FaxReview | None:
        """
        Submit a review.

        Args:
            review_id: Review UUID.
            user_id: User submitting the review.
            corrections: Optional corrections made.

        Returns:
            Submitted FaxReview or None if submit failed.
        """
        now = datetime.now(timezone.utc)

        # Atomic submit guard to prevent stale-claim submissions and races:
        # - review must still be unsubmitted
        # - submitter must be the current claimant
        # - claim must not be expired
        stmt = (
            update(FaxReview)
            .where(
                FaxReview.review_id == review_id,
                FaxReview.submitted_at == None,  # noqa: E711
                FaxReview.claimed_by == user_id,
                (FaxReview.claim_expires_at == None) | (FaxReview.claim_expires_at > now),  # noqa: E711
            )
            .values(
                submitted_by=user_id,
                submitted_at=now,
                corrections=corrections,
            )
        )
        result = self.db.execute(stmt)
        if result.rowcount <= 0:
            self.db.flush()
            return None

        review = self.get_by_id(review_id)
        if review is None:
            self.db.flush()
            return None

        # Keep timing analytics available for feedback dashboards.
        if review.claimed_at:
            review.time_to_submit_seconds = int((now - review.claimed_at).total_seconds())

        self.db.flush()
        return review

    def release_expired_claims(self) -> int:
        """
        Release all expired claims.

        Returns:
            Number of claims released.
        """
        now = datetime.now(timezone.utc)
        stmt = (
            select(FaxReview)
            .where(
                FaxReview.claimed_at != None,  # noqa: E711
                FaxReview.submitted_at == None,  # noqa: E711
                FaxReview.claim_expires_at < now,
            )
        )
        expired = list(self.db.execute(stmt).scalars().all())

        for review in expired:
            review.claimed_by = None
            review.claimed_at = None
            review.claim_expires_at = None

        self.db.flush()
        return len(expired)


class FeedbackRepository(BaseRepository[FaxFeedback]):
    """Repository for FaxFeedback model."""

    def __init__(self, db: Session):
        super().__init__(db, FaxFeedback)

    def get_by_job(self, fax_job_id: UUID) -> list[FaxFeedback]:
        """Get all feedback for a job."""
        stmt = (
            select(FaxFeedback)
            .where(FaxFeedback.fax_job_id == fax_job_id)
            .order_by(FaxFeedback.created_at)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_by_review(self, review_id: UUID) -> list[FaxFeedback]:
        """Get all feedback from a review."""
        stmt = (
            select(FaxFeedback)
            .where(FaxFeedback.review_id == review_id)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_corrections_for_field(
        self,
        field_key: str,
        limit: int = 100,
    ) -> list[FaxFeedback]:
        """Get correction feedback for a field type."""
        stmt = (
            select(FaxFeedback)
            .where(
                FaxFeedback.field_key == field_key,
                FaxFeedback.feedback_type == "correction",
            )
            .order_by(FaxFeedback.created_at.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())

    def create_correction(
        self,
        fax_job_id: UUID,
        review_id: UUID | None,
        field_key: str,
        original_value: str | None,
        corrected_value: str,
        created_by: str,
    ) -> FaxFeedback:
        """Create a correction feedback entry."""
        feedback = FaxFeedback.from_correction(
            fax_job_id=fax_job_id,
            review_id=review_id,
            field_key=field_key,
            original_value=original_value,
            corrected_value=corrected_value,
            created_by=created_by,
        )
        return self.create(feedback)
