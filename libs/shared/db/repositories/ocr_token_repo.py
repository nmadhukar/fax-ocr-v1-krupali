"""
OCR Token repository - optimized for bulk operations.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from libs.shared.db.models.fax_ocr_token import FaxOcrToken
from libs.shared.db.repositories.base import BaseRepository


class OcrTokenRepository(BaseRepository[FaxOcrToken]):
    """
    Repository for FaxOcrToken model.

    Optimized for bulk insert operations since this table
    will contain millions of rows.
    """

    def __init__(self, db: Session):
        super().__init__(db, FaxOcrToken)

    def get_by_page(
        self,
        fax_page_id: UUID,
        skip: int = 0,
        limit: int = 10000,
    ) -> list[FaxOcrToken]:
        """
        Get all tokens for a page.

        Args:
            fax_page_id: Page UUID.
            skip: Records to skip.
            limit: Maximum records to return.

        Returns:
            List of FaxOcrToken instances in reading order.
        """
        stmt = (
            select(FaxOcrToken)
            .where(FaxOcrToken.fax_page_id == fax_page_id)
            .order_by(FaxOcrToken.line_number, FaxOcrToken.word_number)
            .offset(skip)
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_tokens_in_roi(
        self,
        fax_page_id: UUID,
        roi_x0: float,
        roi_y0: float,
        roi_x1: float,
        roi_y1: float,
        overlap_threshold: float = 0.5,
    ) -> list[FaxOcrToken]:
        """
        Get tokens that overlap with a Region of Interest.

        Uses a bounding box query for efficiency, then filters
        by overlap threshold in Python.

        Args:
            fax_page_id: Page UUID.
            roi_x0: ROI left edge (normalized).
            roi_y0: ROI top edge (normalized).
            roi_x1: ROI right edge (normalized).
            roi_y1: ROI bottom edge (normalized).
            overlap_threshold: Minimum overlap ratio.

        Returns:
            List of overlapping FaxOcrToken instances.
        """
        # First, get tokens that could potentially overlap (loose filter)
        # This uses the bbox index for efficiency
        stmt = (
            select(FaxOcrToken)
            .where(
                FaxOcrToken.fax_page_id == fax_page_id,
                FaxOcrToken.bbox_x1 >= roi_x0,
                FaxOcrToken.bbox_x0 <= roi_x1,
                FaxOcrToken.bbox_y1 >= roi_y0,
                FaxOcrToken.bbox_y0 <= roi_y1,
            )
            .order_by(FaxOcrToken.bbox_y0, FaxOcrToken.bbox_x0)
        )
        candidates = list(self.db.execute(stmt).scalars().all())

        # Filter by actual overlap threshold
        if overlap_threshold <= 0:
            return candidates

        return [
            token for token in candidates
            if token.overlaps_roi(roi_x0, roi_y0, roi_x1, roi_y1, overlap_threshold)
        ]

    def get_tokens_inside_roi(
        self,
        fax_page_id: UUID,
        roi_x0: float,
        roi_y0: float,
        roi_x1: float,
        roi_y1: float,
    ) -> list[FaxOcrToken]:
        """
        Get tokens completely inside a Region of Interest.

        Args:
            fax_page_id: Page UUID.
            roi_x0: ROI left edge (normalized).
            roi_y0: ROI top edge (normalized).
            roi_x1: ROI right edge (normalized).
            roi_y1: ROI bottom edge (normalized).

        Returns:
            List of FaxOcrToken instances fully inside ROI.
        """
        stmt = (
            select(FaxOcrToken)
            .where(
                FaxOcrToken.fax_page_id == fax_page_id,
                FaxOcrToken.bbox_x0 >= roi_x0,
                FaxOcrToken.bbox_y0 >= roi_y0,
                FaxOcrToken.bbox_x1 <= roi_x1,
                FaxOcrToken.bbox_y1 <= roi_y1,
            )
            .order_by(FaxOcrToken.bbox_y0, FaxOcrToken.bbox_x0)
        )
        return list(self.db.execute(stmt).scalars().all())

    def bulk_insert(self, tokens: list[dict[str, Any]]) -> int:
        """
        Bulk insert OCR tokens for performance.

        Args:
            tokens: List of token dictionaries with columns:
                - fax_page_id
                - token_text
                - line_number
                - word_number
                - bbox_x0, bbox_y0, bbox_x1, bbox_y1
                - confidence
                - is_numeric (optional)
                - is_date_like (optional)

        Returns:
            Number of tokens inserted.
        """
        if not tokens:
            return 0

        # Use PostgreSQL INSERT for efficiency
        stmt = insert(FaxOcrToken).values(tokens)
        self.db.execute(stmt)
        self.db.flush()
        return len(tokens)

    def delete_by_page(self, fax_page_id: UUID) -> int:
        """
        Delete all tokens for a page in a single bulk DELETE.

        Args:
            fax_page_id: Page UUID.

        Returns:
            Number of tokens deleted.
        """
        stmt = delete(FaxOcrToken).where(FaxOcrToken.fax_page_id == fax_page_id)
        result = self.db.execute(stmt)
        self.db.flush()
        return result.rowcount

    def count_by_page(self, fax_page_id: UUID) -> int:
        """Count tokens for a page."""
        stmt = (
            select(func.count())
            .select_from(FaxOcrToken)
            .where(FaxOcrToken.fax_page_id == fax_page_id)
        )
        return self.db.execute(stmt).scalar() or 0

    def search_text(
        self,
        fax_page_id: UUID,
        text: str,
        case_sensitive: bool = False,
    ) -> list[FaxOcrToken]:
        """
        Search for tokens containing specific text.

        Args:
            fax_page_id: Page UUID.
            text: Text to search for.
            case_sensitive: Whether search is case sensitive.

        Returns:
            List of matching FaxOcrToken instances.
        """
        if case_sensitive:
            condition = FaxOcrToken.token_text.contains(text)
        else:
            # Escape LIKE special characters to prevent pattern injection
            escaped = text.replace("%", r"\%").replace("_", r"\_")
            condition = FaxOcrToken.token_text.ilike(f"%{escaped}%")

        stmt = (
            select(FaxOcrToken)
            .where(
                FaxOcrToken.fax_page_id == fax_page_id,
                condition,
            )
            .order_by(FaxOcrToken.line_number, FaxOcrToken.word_number)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_numeric_tokens(self, fax_page_id: UUID) -> list[FaxOcrToken]:
        """Get all numeric tokens on a page."""
        stmt = (
            select(FaxOcrToken)
            .where(
                FaxOcrToken.fax_page_id == fax_page_id,
                FaxOcrToken.is_numeric == True,
            )
            .order_by(FaxOcrToken.line_number, FaxOcrToken.word_number)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_date_tokens(self, fax_page_id: UUID) -> list[FaxOcrToken]:
        """Get all date-like tokens on a page."""
        stmt = (
            select(FaxOcrToken)
            .where(
                FaxOcrToken.fax_page_id == fax_page_id,
                FaxOcrToken.is_date_like == True,
            )
            .order_by(FaxOcrToken.line_number, FaxOcrToken.word_number)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_page_text(self, fax_page_id: UUID) -> str:
        """
        Get full text content of a page.

        Args:
            fax_page_id: Page UUID.

        Returns:
            Concatenated text with line breaks.
        """
        tokens = self.get_by_page(fax_page_id)

        lines: dict[int, list[str]] = {}
        for token in tokens:
            if token.line_number not in lines:
                lines[token.line_number] = []
            lines[token.line_number].append(token.token_text)

        return "\n".join(
            " ".join(words) for _, words in sorted(lines.items())
        )

    def get_average_confidence(self, fax_page_id: UUID) -> float:
        """Get average OCR confidence for a page."""
        stmt = (
            select(func.avg(FaxOcrToken.confidence))
            .where(FaxOcrToken.fax_page_id == fax_page_id)
        )
        result = self.db.execute(stmt).scalar()
        return float(result) if result else 0.0
