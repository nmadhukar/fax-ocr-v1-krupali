"""
FaxJob model - Core entity tracking for fax documents.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, Enum, ForeignKey, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.shared.db.base import Base
from libs.shared.db.models.enums import DocTypeEnum, FaxJobStatusEnum, PayerNameEnum

if TYPE_CHECKING:
    from libs.shared.db.models.fax_extraction import FaxExtractedField, FaxExtraction
    from libs.shared.db.models.fax_page import FaxPage
    from libs.shared.db.models.fax_review import FaxReview
    from libs.shared.db.models.fax_template import FaxTemplateVersion


class FaxJob(Base):
    """
    Core entity tracking for incoming fax documents.

    Represents a single fax submission that goes through the
    OCR → Template Match → Extract → Review pipeline.
    """

    __tablename__ = "fax_job"

    # Primary key
    fax_job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("uuid_generate_v4()"),
    )

    # Tenant and file info
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_pages: Mapped[int | None] = mapped_column(default=None)

    # Classification
    payer_hint: Mapped[PayerNameEnum] = mapped_column(
        Enum(PayerNameEnum, name="payer_name_enum", create_type=False),
        default=PayerNameEnum.UNKNOWN,
    )
    doc_type: Mapped[DocTypeEnum] = mapped_column(
        Enum(DocTypeEnum, name="doc_type_enum", create_type=False),
        default=DocTypeEnum.UNKNOWN,
    )
    doc_type_conf: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        default=None,
    )

    # Template matching
    matched_template_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fax_template_version.template_version_id", ondelete="SET NULL"),
        default=None,
    )
    matched_template_score: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        default=None,
    )

    # Status and confidence
    status: Mapped[FaxJobStatusEnum] = mapped_column(
        Enum(FaxJobStatusEnum, name="fax_job_status_enum", create_type=False),
        default=FaxJobStatusEnum.PENDING,
        index=True,
    )
    overall_conf: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        default=None,
    )
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=text("NOW()"),
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(default=None)
    processing_completed_at: Mapped[datetime | None] = mapped_column(default=None)

    # External reference
    external_fax_id: Mapped[str | None] = mapped_column(String(255), default=None)

    # Flexible metadata (named job_metadata to avoid SQLAlchemy reserved name conflict)
    job_metadata: Mapped[dict] = mapped_column(
        "metadata",  # Keep the actual DB column name as "metadata"
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    # Relationships
    pages: Mapped[list["FaxPage"]] = relationship(
        "FaxPage",
        back_populates="fax_job",
        cascade="all, delete-orphan",
        order_by="FaxPage.page_number",
    )

    extracted_fields: Mapped[list["FaxExtractedField"]] = relationship(
        "FaxExtractedField",
        back_populates="fax_job",
        cascade="all, delete-orphan",
    )

    extraction: Mapped["FaxExtraction | None"] = relationship(
        "FaxExtraction",
        back_populates="fax_job",
        uselist=False,
        cascade="all, delete-orphan",
    )

    review: Mapped["FaxReview | None"] = relationship(
        "FaxReview",
        back_populates="fax_job",
        uselist=False,
        cascade="all, delete-orphan",
    )

    @property
    def is_completed(self) -> bool:
        """Check if job processing is complete."""
        return self.status == FaxJobStatusEnum.COMPLETED

    @property
    def is_failed(self) -> bool:
        """Check if job processing failed."""
        return self.status == FaxJobStatusEnum.FAILED

    @property
    def processing_duration_seconds(self) -> float | None:
        """Calculate processing duration in seconds."""
        if self.processing_started_at and self.processing_completed_at:
            delta = self.processing_completed_at - self.processing_started_at
            return delta.total_seconds()
        return None
