"""
Review models - Human review workflow and feedback.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Integer, String, Text, text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.shared.db.base import Base

if TYPE_CHECKING:
    from libs.shared.db.models.fax_job import FaxJob


class FaxReview(Base):
    """
    Human review workflow for uncertain extractions.

    Tracks claim/submit workflow with timing metrics
    for performance analysis.
    """

    __tablename__ = "fax_review"

    __table_args__ = (
        UniqueConstraint("fax_job_id", name="uq_review_job"),
    )

    # Primary key
    review_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("uuid_generate_v4()"),
    )

    # Foreign key to parent job
    fax_job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fax_job.fax_job_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # Review packet (everything needed for review UI)
    review_packet: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="Complete review data including pages and fields",
    )
    review_reasons: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text),
        default=None,
        comment="Reasons for requiring review",
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        default=0,
        comment="Higher priority = more urgent",
    )

    # Claim workflow
    claimed_by: Mapped[str | None] = mapped_column(
        String(255),
        default=None,
        comment="Reviewer who claimed this review",
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        default=None,
        comment="When the review was claimed",
    )
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        default=None,
        comment="Claim expiration time",
    )

    # Submission
    submitted_by: Mapped[str | None] = mapped_column(
        String(255),
        default=None,
        comment="Reviewer who submitted",
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        default=None,
        comment="When review was submitted",
    )
    corrections: Mapped[dict | None] = mapped_column(
        JSONB,
        default=None,
        comment="Corrections made by reviewer",
    )

    # Timing metrics
    time_to_claim_seconds: Mapped[int | None] = mapped_column(
        Integer,
        default=None,
        comment="Seconds from creation to claim",
    )
    time_to_submit_seconds: Mapped[int | None] = mapped_column(
        Integer,
        default=None,
        comment="Seconds from claim to submit",
    )

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow,
        server_default=text("NOW()"),
    )

    # Relationship
    fax_job: Mapped["FaxJob"] = relationship(
        "FaxJob",
        back_populates="review",
    )

    feedbacks: Mapped[list["FaxFeedback"]] = relationship(
        "FaxFeedback",
        back_populates="review",
        cascade="all, delete-orphan",
    )

    @property
    def is_claimed(self) -> bool:
        """Check if review is currently claimed."""
        return self.claimed_by is not None and self.submitted_at is None

    @property
    def is_submitted(self) -> bool:
        """Check if review has been submitted."""
        return self.submitted_at is not None

    @property
    def is_expired(self) -> bool:
        """Check if claim has expired."""
        if not self.is_claimed or not self.claim_expires_at:
            return False
        return datetime.now(timezone.utc) > self.claim_expires_at

    def claim(
        self,
        reviewer_id: str,
        expiry_minutes: int = 30,
    ) -> bool:
        """
        Claim this review for a reviewer.

        Args:
            reviewer_id: ID of the reviewer.
            expiry_minutes: Minutes until claim expires.

        Returns:
            True if claim was successful.
        """
        if self.is_claimed and not self.is_expired:
            return False

        from datetime import timedelta
        now = datetime.now(timezone.utc)

        self.claimed_by = reviewer_id
        self.claimed_at = now
        self.claim_expires_at = now + timedelta(minutes=expiry_minutes)

        # Calculate time to claim
        if self.created_at:
            self.time_to_claim_seconds = int((now - self.created_at).total_seconds())

        return True

    def submit(
        self,
        reviewer_id: str,
        corrections: dict | None = None,
    ) -> bool:
        """
        Submit the review.

        Args:
            reviewer_id: ID of the submitting reviewer.
            corrections: Optional corrections made.

        Returns:
            True if submission was successful.
        """
        if not self.is_claimed:
            return False
        if self.claimed_by != reviewer_id:
            return False

        now = datetime.now(timezone.utc)
        self.submitted_by = reviewer_id
        self.submitted_at = now
        self.corrections = corrections

        # Calculate time to submit
        if self.claimed_at:
            self.time_to_submit_seconds = int((now - self.claimed_at).total_seconds())

        return True


class FaxFeedback(Base):
    """
    Feedback data for model improvement and training.

    Records corrections made during review for the feedback loop.
    """

    __tablename__ = "fax_feedback"

    # Primary key
    feedback_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("uuid_generate_v4()"),
    )

    # Foreign keys
    fax_job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fax_job.fax_job_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    review_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fax_review.review_id", ondelete="SET NULL"),
        default=None,
    )

    # Field identification
    field_key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Field that was corrected",
    )

    # Original values
    original_value: Mapped[str | None] = mapped_column(
        Text,
        default=None,
        comment="Value before correction",
    )
    original_bbox: Mapped[dict | None] = mapped_column(
        JSONB,
        default=None,
        comment="Original bounding box",
    )

    # Corrected values
    corrected_value: Mapped[str | None] = mapped_column(
        Text,
        default=None,
        comment="Value after correction",
    )
    corrected_bbox: Mapped[dict | None] = mapped_column(
        JSONB,
        default=None,
        comment="Corrected bounding box",
    )

    # Feedback metadata
    feedback_type: Mapped[str | None] = mapped_column(
        String(50),
        default=None,
        comment="correction, confirmation, rejection",
    )
    feedback_source: Mapped[str | None] = mapped_column(
        String(50),
        default=None,
        comment="human_review, api_correction, auto_validation",
    )

    # Audit
    created_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow,
        server_default=text("NOW()"),
    )
    created_by: Mapped[str | None] = mapped_column(
        String(255),
        default=None,
    )

    # Relationship
    review: Mapped["FaxReview | None"] = relationship(
        "FaxReview",
        back_populates="feedbacks",
    )

    @classmethod
    def from_correction(
        cls,
        fax_job_id: UUID,
        review_id: UUID | None,
        field_key: str,
        original_value: str | None,
        corrected_value: str,
        created_by: str,
    ) -> "FaxFeedback":
        """
        Create a feedback record from a correction.

        Args:
            fax_job_id: Parent fax job ID.
            review_id: Optional parent review ID.
            field_key: Field that was corrected.
            original_value: Value before correction.
            corrected_value: Value after correction.
            created_by: User who made the correction.

        Returns:
            FaxFeedback instance.
        """
        return cls(
            fax_job_id=fax_job_id,
            review_id=review_id,
            field_key=field_key,
            original_value=original_value,
            corrected_value=corrected_value,
            feedback_type="correction",
            feedback_source="human_review",
            created_by=created_by,
        )
