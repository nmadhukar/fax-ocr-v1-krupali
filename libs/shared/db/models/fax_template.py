"""
Template models - Definitions for payer-specific document formats.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.shared.db.base import Base
from libs.shared.db.models.enums import DocTypeEnum, PayerNameEnum

if TYPE_CHECKING:
    pass


class FaxTemplate(Base):
    """
    Template definitions for payer-specific document formats.

    A template groups multiple versions for A/B testing
    and safe rollback of template changes.
    """

    __tablename__ = "fax_template"

    __table_args__ = (
        UniqueConstraint(
            "payer_name", "doc_type", "template_name",
            name="uq_template_payer_doctype_name"
        ),
    )

    # Primary key
    template_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("uuid_generate_v4()"),
    )

    # Template identification
    payer_name: Mapped[PayerNameEnum] = mapped_column(
        Enum(PayerNameEnum, name="payer_name_enum", create_type=False),
        nullable=False,
        index=True,
    )
    doc_type: Mapped[DocTypeEnum] = mapped_column(
        Enum(DocTypeEnum, name="doc_type_enum", create_type=False),
        nullable=False,
    )
    template_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Audit
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=text("NOW()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=text("NOW()"),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    created_by: Mapped[str | None] = mapped_column(String(255), default=None)

    # Relationships
    versions: Mapped[list["FaxTemplateVersion"]] = relationship(
        "FaxTemplateVersion",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="FaxTemplateVersion.created_at.desc()",
    )

    @property
    def active_version(self) -> "FaxTemplateVersion | None":
        """Get the currently active version."""
        for version in self.versions:
            if version.is_active:
                return version
        return None


class FaxTemplateVersion(Base):
    """
    Versioned template configurations for safe updates.

    Each version contains matching thresholds and can be
    activated/deactivated independently.
    """

    __tablename__ = "fax_template_version"

    __table_args__ = (
        UniqueConstraint(
            "template_id", "version_label",
            name="uq_template_version_label"
        ),
    )

    # Primary key
    template_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("uuid_generate_v4()"),
    )

    # Foreign key to parent template
    template_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fax_template.template_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Version identification
    version_label: Mapped[str] = mapped_column(String(50), nullable=False)

    # Matching thresholds
    match_min_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 4),
        default=Decimal("0.75"),
        comment="Minimum combined match score (0.0-1.0)",
    )
    match_phash_threshold: Mapped[int] = mapped_column(
        Integer,
        default=10,
        comment="Maximum Hamming distance for pHash match",
    )
    match_orb_min_matches: Mapped[int] = mapped_column(
        Integer,
        default=20,
        comment="Minimum ORB feature matches required",
    )

    # Template configuration (rotate_pages, content_pages, cover_pages)
    template_config: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        default=None,
        comment="Template metadata: rotate_pages, content_pages, cover_pages",
    )

    # Status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        comment="Only one version should be active per template",
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=text("NOW()"),
    )
    activated_at: Mapped[datetime | None] = mapped_column(default=None)

    # Relationships
    template: Mapped["FaxTemplate"] = relationship(
        "FaxTemplate",
        back_populates="versions",
    )

    samples: Mapped[list["FaxTemplateSample"]] = relationship(
        "FaxTemplateSample",
        back_populates="template_version",
        cascade="all, delete-orphan",
    )

    fields: Mapped[list["FaxTemplateField"]] = relationship(
        "FaxTemplateField",
        back_populates="template_version",
        cascade="all, delete-orphan",
    )


class FaxTemplateSample(Base):
    """
    Sample images with precomputed matching features.

    Multiple samples per version improve matching accuracy
    across form variations.
    """

    __tablename__ = "fax_template_sample"

    # Primary key
    sample_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("uuid_generate_v4()"),
    )

    # Foreign key to parent version
    template_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fax_template_version.template_version_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Storage
    sample_storage_key: Mapped[str] = mapped_column(String(512), nullable=False)

    # Matching features
    phash_value: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
        comment="Perceptual hash for fast prefiltering",
    )
    orb_descriptors: Mapped[bytes | None] = mapped_column(
        LargeBinary,
        default=None,
        comment="Serialized ORB feature descriptors",
    )
    orb_keypoints: Mapped[bytes | None] = mapped_column(
        LargeBinary,
        default=None,
        comment="Serialized ORB keypoints",
    )

    # Image dimensions
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=text("NOW()"),
    )

    # Relationship
    template_version: Mapped["FaxTemplateVersion"] = relationship(
        "FaxTemplateVersion",
        back_populates="samples",
    )


class FaxTemplateField(Base):
    """
    Field definitions with ROI coordinates for template extraction.

    Each field defines a region of interest where the
    field value is expected to be found.
    """

    __tablename__ = "fax_template_field"

    __table_args__ = (
        UniqueConstraint(
            "template_version_id", "field_key",
            name="uq_template_field_key"
        ),
    )

    # Primary key
    template_field_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("uuid_generate_v4()"),
    )

    # Foreign key to parent version
    template_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fax_template_version.template_version_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Field identification
    field_key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Canonical field key (e.g., 'member_id')",
    )
    field_label: Mapped[str | None] = mapped_column(
        String(255),
        default=None,
        comment="Human-readable label",
    )
    is_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        comment="True if field must be extracted",
    )

    # Normalized ROI coordinates [0.0 - 1.0]
    roi_x0: Mapped[Decimal] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        comment="Left edge (0.0-1.0)",
    )
    roi_y0: Mapped[Decimal] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        comment="Top edge (0.0-1.0)",
    )
    roi_x1: Mapped[Decimal] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        comment="Right edge (0.0-1.0)",
    )
    roi_y1: Mapped[Decimal] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        comment="Bottom edge (0.0-1.0)",
    )

    # Target page (default first page)
    target_page: Mapped[int] = mapped_column(
        Integer,
        default=1,
        comment="Page number where field is expected (1-indexed)",
    )

    # Validation
    validation_regex: Mapped[str | None] = mapped_column(
        Text,
        default=None,
        comment="Regex pattern for validation",
    )
    validation_message: Mapped[str | None] = mapped_column(
        Text,
        default=None,
        comment="Error message for validation failures",
    )

    # Extraction hints
    expected_type: Mapped[str] = mapped_column(
        String(50),
        default="text",
        comment="Expected type: text, date, number, phone, etc.",
    )
    post_processing: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Post-processing rules",
    )

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=text("NOW()"),
    )

    # Relationship
    template_version: Mapped["FaxTemplateVersion"] = relationship(
        "FaxTemplateVersion",
        back_populates="fields",
    )

    @property
    def roi_width(self) -> Decimal:
        """Calculate ROI width."""
        return self.roi_x1 - self.roi_x0

    @property
    def roi_height(self) -> Decimal:
        """Calculate ROI height."""
        return self.roi_y1 - self.roi_y0

    def to_roi_dict(self) -> dict[str, float]:
        """Convert ROI to dictionary."""
        return {
            "x0": float(self.roi_x0),
            "y0": float(self.roi_y0),
            "x1": float(self.roi_x1),
            "y1": float(self.roi_y1),
        }
