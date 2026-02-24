"""
Extraction models - Extracted field values and final results.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, BIGINT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.shared.db.base import Base
from libs.shared.db.models.enums import ExtractionMethodEnum

if TYPE_CHECKING:
    from libs.shared.db.models.fax_job import FaxJob


class FaxExtractedField(Base):
    """
    Extracted field values with method attribution and evidence.

    Each field can have multiple extraction candidates from
    different methods (template, VLM, etc.).
    """

    __tablename__ = "fax_extracted_field"

    __table_args__ = (
        UniqueConstraint(
            "fax_job_id", "field_key", "method",
            name="uq_extracted_field_job_key_method"
        ),
    )

    # Primary key
    extracted_field_id: Mapped[UUID] = mapped_column(
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
        index=True,
    )

    # Field identification
    field_key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Canonical field key (e.g., 'member_id')",
    )
    field_value: Mapped[str | None] = mapped_column(
        Text,
        default=None,
        comment="Extracted field value",
    )

    # Extraction method
    method: Mapped[ExtractionMethodEnum] = mapped_column(
        Enum(ExtractionMethodEnum, name="extraction_method_enum", create_type=False),
        nullable=False,
        comment="Method used for extraction",
    )
    field_conf: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        default=None,
        comment="Confidence score (0.0-1.0)",
    )

    # Evidence linking
    evidence_bbox: Mapped[dict | None] = mapped_column(
        JSONB,
        default=None,
        comment="Bounding box: {page, x0, y0, x1, y1}",
    )
    evidence_text: Mapped[str | None] = mapped_column(
        Text,
        default=None,
        comment="Raw OCR text used as evidence",
    )
    evidence_token_ids: Mapped[list[int] | None] = mapped_column(
        ARRAY(BIGINT),
        default=None,
        comment="OCR token IDs that form this field",
    )

    # Multiple extraction candidates
    candidates: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment="All extraction candidates with scores",
    )

    # Validation result
    validation_passed: Mapped[bool | None] = mapped_column(
        Boolean,
        default=None,
        comment="True if field passed validation",
    )
    validation_errors: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text),
        default=None,
        comment="Validation error messages",
    )

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=text("NOW()"),
    )

    # Relationship
    fax_job: Mapped["FaxJob"] = relationship(
        "FaxJob",
        back_populates="extracted_fields",
    )

    def add_candidate(
        self,
        value: str,
        method: str,
        confidence: float,
        evidence_bbox: dict[str, Any] | None = None,
    ) -> None:
        """
        Add an extraction candidate.

        Args:
            value: Extracted value.
            method: Extraction method used.
            confidence: Confidence score.
            evidence_bbox: Optional bounding box evidence.
        """
        candidate = {
            "value": value,
            "method": method,
            "confidence": confidence,
        }
        if evidence_bbox:
            candidate["evidence_bbox"] = evidence_bbox

        if self.candidates is None:
            self.candidates = []
        self.candidates = [*self.candidates, candidate]

    def get_best_candidate(self) -> dict[str, Any] | None:
        """
        Get the highest confidence candidate.

        Returns:
            Best candidate dict or None.
        """
        if not self.candidates:
            return None

        return max(
            self.candidates,
            key=lambda c: c.get("confidence", 0),
        )


class FaxExtraction(Base):
    """
    Final merged extraction result with version metadata.

    Contains the consolidated extraction JSON and quality metrics.
    """

    __tablename__ = "fax_extraction"

    __table_args__ = (
        UniqueConstraint("fax_job_id", name="uq_extraction_job"),
    )

    # Primary key
    extraction_id: Mapped[UUID] = mapped_column(
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

    # Extraction result
    extraction_json: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="Final extracted fields as JSON",
    )

    # Version tracking
    model_versions: Mapped[dict | None] = mapped_column(
        JSONB,
        default=None,
        comment="Model versions used: {ocr, vlm, template, ...}",
    )
    pipeline_version: Mapped[str | None] = mapped_column(
        String(50),
        default=None,
        comment="Processing pipeline version",
    )

    # HITL review flags — populated by compute_field_flags() after pipeline step 16
    flagged_fields: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment="Per-field HITL flags: [{field_key, reason, threshold, confidence}]",
    )

    # Quality metrics
    total_fields: Mapped[int | None] = mapped_column(
        Integer,
        default=None,
        comment="Total number of fields extracted",
    )
    high_conf_fields: Mapped[int | None] = mapped_column(
        Integer,
        default=None,
        comment="Fields with confidence >= 0.85",
    )
    low_conf_fields: Mapped[int | None] = mapped_column(
        Integer,
        default=None,
        comment="Fields with confidence < 0.70",
    )
    missing_fields: Mapped[int | None] = mapped_column(
        Integer,
        default=None,
        comment="Required fields that could not be extracted",
    )

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=text("NOW()"),
    )

    # Relationship
    fax_job: Mapped["FaxJob"] = relationship(
        "FaxJob",
        back_populates="extraction",
    )

    def get_field(self, field_key: str) -> Any:
        """
        Get a field value from the extraction JSON.

        Args:
            field_key: Field key to retrieve.

        Returns:
            Field value or None.
        """
        return self.extraction_json.get(field_key)

    def calculate_metrics(self, required_fields: list[str]) -> None:
        """
        Calculate and update quality metrics.

        Args:
            required_fields: List of required field keys.
        """
        if not self.extraction_json:
            return

        self.total_fields = len(self.extraction_json)
        self.high_conf_fields = 0
        self.low_conf_fields = 0
        self.missing_fields = 0

        for field in self.extraction_json.values():
            if isinstance(field, dict):
                conf = field.get("confidence", 0)
                if conf >= 0.85:
                    self.high_conf_fields += 1
                elif conf < 0.70:
                    self.low_conf_fields += 1

        for req_field in required_fields:
            if req_field not in self.extraction_json:
                self.missing_fields += 1
