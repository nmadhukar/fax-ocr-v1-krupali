"""
FaxOcrToken model - OCR tokens with bounding boxes.

This is a critical table that will contain millions of rows.
Each token represents a word/text fragment from OCR.
"""

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.shared.db.base import Base

if TYPE_CHECKING:
    from libs.shared.db.models.fax_page import FaxPage


class FaxOcrToken(Base):
    """
    OCR tokens with normalized bounding boxes.

    Each token represents a recognized text fragment with its
    position (normalized to 0.0-1.0) and confidence score.
    This table is critical for evidence linking in extractions.
    """

    __tablename__ = "fax_ocr_token"

    # Primary key (BIGSERIAL for high volume)
    ocr_token_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    # Foreign key to parent page
    fax_page_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fax_page.fax_page_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Token text content
    token_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Position in reading order
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    word_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Normalized bounding box [0.0 - 1.0]
    # These represent percentages of page dimensions
    bbox_x0: Mapped[Decimal] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        comment="Left edge (0.0-1.0)",
    )
    bbox_y0: Mapped[Decimal] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        comment="Top edge (0.0-1.0)",
    )
    bbox_x1: Mapped[Decimal] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        comment="Right edge (0.0-1.0)",
    )
    bbox_y1: Mapped[Decimal] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        comment="Bottom edge (0.0-1.0)",
    )

    # OCR confidence score
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(5, 4),
        nullable=False,
        comment="OCR confidence (0.0-1.0)",
    )

    # Token type hints (computed during OCR)
    is_numeric: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        comment="True if token appears to be a number",
    )
    is_date_like: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        comment="True if token appears to be part of a date",
    )

    # Optional metadata
    font_size_estimate: Mapped[int | None] = mapped_column(
        Integer,
        default=None,
        comment="Estimated font size in points",
    )
    is_bold: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        comment="True if token appears bold",
    )
    is_handwritten: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        comment="True if token appears handwritten",
    )

    # Relationship
    fax_page: Mapped["FaxPage"] = relationship(
        "FaxPage",
        back_populates="ocr_tokens",
    )

    @property
    def bbox_width(self) -> Decimal:
        """Calculate normalized bounding box width."""
        return self.bbox_x1 - self.bbox_x0

    @property
    def bbox_height(self) -> Decimal:
        """Calculate normalized bounding box height."""
        return self.bbox_y1 - self.bbox_y0

    @property
    def bbox_center_x(self) -> Decimal:
        """Calculate center X coordinate."""
        return (self.bbox_x0 + self.bbox_x1) / 2

    @property
    def bbox_center_y(self) -> Decimal:
        """Calculate center Y coordinate."""
        return (self.bbox_y0 + self.bbox_y1) / 2

    def get_absolute_bbox(self, page_width: int, page_height: int) -> dict[str, int]:
        """
        Convert normalized bbox to absolute pixel coordinates.

        Args:
            page_width: Page width in pixels.
            page_height: Page height in pixels.

        Returns:
            Dict with x0, y0, x1, y1 in pixels.
        """
        return {
            "x0": int(float(self.bbox_x0) * page_width),
            "y0": int(float(self.bbox_y0) * page_height),
            "x1": int(float(self.bbox_x1) * page_width),
            "y1": int(float(self.bbox_y1) * page_height),
        }

    def overlaps_roi(
        self,
        roi_x0: float,
        roi_y0: float,
        roi_x1: float,
        roi_y1: float,
        threshold: float = 0.5,
    ) -> bool:
        """
        Check if token overlaps with a Region of Interest.

        Args:
            roi_x0: ROI left edge (normalized).
            roi_y0: ROI top edge (normalized).
            roi_x1: ROI right edge (normalized).
            roi_y1: ROI bottom edge (normalized).
            threshold: Minimum overlap ratio to consider as overlapping.

        Returns:
            True if token overlaps with ROI above threshold.
        """
        # Calculate intersection
        x_overlap = max(
            0,
            min(float(self.bbox_x1), roi_x1) - max(float(self.bbox_x0), roi_x0)
        )
        y_overlap = max(
            0,
            min(float(self.bbox_y1), roi_y1) - max(float(self.bbox_y0), roi_y0)
        )

        intersection = x_overlap * y_overlap
        token_area = float(self.bbox_width) * float(self.bbox_height)

        if token_area == 0:
            return False

        overlap_ratio = intersection / token_area
        return overlap_ratio >= threshold

    def is_inside_roi(
        self,
        roi_x0: float,
        roi_y0: float,
        roi_x1: float,
        roi_y1: float,
    ) -> bool:
        """
        Check if token is completely inside a Region of Interest.

        Args:
            roi_x0: ROI left edge (normalized).
            roi_y0: ROI top edge (normalized).
            roi_x1: ROI right edge (normalized).
            roi_y1: ROI bottom edge (normalized).

        Returns:
            True if token is fully contained within ROI.
        """
        return (
            float(self.bbox_x0) >= roi_x0
            and float(self.bbox_y0) >= roi_y0
            and float(self.bbox_x1) <= roi_x1
            and float(self.bbox_y1) <= roi_y1
        )
