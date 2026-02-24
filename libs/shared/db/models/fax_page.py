"""
FaxPage model - Individual pages from fax documents.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.shared.db.base import Base

if TYPE_CHECKING:
    from libs.shared.db.models.fax_job import FaxJob
    from libs.shared.db.models.fax_ocr_token import FaxOcrToken


class FaxPage(Base):
    """
    Individual pages extracted from fax documents.

    Stores page-level metadata including quality metrics
    used for preprocessing decisions.
    """

    __tablename__ = "fax_page"

    __table_args__ = (
        UniqueConstraint("fax_job_id", "page_number", name="uq_fax_page_job_number"),
    )

    # Primary key
    fax_page_id: Mapped[UUID] = mapped_column(
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

    # Page identification
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    page_storage_key: Mapped[str] = mapped_column(String(512), nullable=False)

    # Page dimensions
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)
    dpi: Mapped[int | None] = mapped_column(Integer, default=None)

    # Quality metrics (computed during preprocessing)
    blur_score: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4),
        default=None,
        comment="Laplacian variance - higher is sharper",
    )
    skew_angle_deg: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 3),
        default=None,
        comment="Detected skew angle in degrees",
    )
    text_density: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        default=None,
        comment="Ratio of text pixels (0.0-1.0)",
    )
    is_cover_page: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        comment="True if detected as cover/transmittal page",
    )

    # Preprocessing tracking
    preprocessed_storage_key: Mapped[str | None] = mapped_column(
        String(512),
        default=None,
        comment="Storage key for preprocessed image",
    )
    preprocessing_applied: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment="List of preprocessing steps applied",
    )

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=text("NOW()"),
    )

    # Relationships
    fax_job: Mapped["FaxJob"] = relationship(
        "FaxJob",
        back_populates="pages",
    )

    ocr_tokens: Mapped[list["FaxOcrToken"]] = relationship(
        "FaxOcrToken",
        back_populates="fax_page",
        cascade="all, delete-orphan",
        order_by="FaxOcrToken.line_number, FaxOcrToken.word_number",
    )

    @property
    def aspect_ratio(self) -> float:
        """Calculate page aspect ratio (width/height)."""
        if self.height_px > 0:
            return self.width_px / self.height_px
        return 0.0

    @property
    def is_landscape(self) -> bool:
        """Check if page is landscape orientation."""
        return self.width_px > self.height_px

    @property
    def is_low_quality(self) -> bool:
        """
        Check if page has low quality based on metrics.

        Low quality threshold: blur_score < 100
        """
        if self.blur_score is None:
            return False
        return float(self.blur_score) < 100.0

    def get_full_text(self) -> str:
        """
        Get concatenated text from all OCR tokens.

        Returns:
            Full page text with proper line breaks.
        """
        if not self.ocr_tokens:
            return ""

        lines: dict[int, list[str]] = {}
        for token in self.ocr_tokens:
            if token.line_number not in lines:
                lines[token.line_number] = []
            lines[token.line_number].append(token.token_text)

        return "\n".join(
            " ".join(words) for _, words in sorted(lines.items())
        )
