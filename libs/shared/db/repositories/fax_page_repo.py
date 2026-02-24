"""
FaxPage repository.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from libs.shared.db.models.fax_page import FaxPage
from libs.shared.db.repositories.base import BaseRepository


class FaxPageRepository(BaseRepository[FaxPage]):
    """Repository for FaxPage model."""

    def __init__(self, db: Session):
        super().__init__(db, FaxPage)

    def get_by_job(self, fax_job_id: UUID) -> list[FaxPage]:
        """
        Get all pages for a fax job.

        Args:
            fax_job_id: Parent fax job UUID.

        Returns:
            List of FaxPage instances ordered by page number.
        """
        stmt = (
            select(FaxPage)
            .where(FaxPage.fax_job_id == fax_job_id)
            .order_by(FaxPage.page_number)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_page_with_tokens(
        self,
        fax_job_id: UUID,
        page_number: int,
    ) -> FaxPage | None:
        """
        Get a specific page with OCR tokens.

        Args:
            fax_job_id: Parent fax job UUID.
            page_number: Page number (1-indexed).

        Returns:
            FaxPage with tokens or None.
        """
        stmt = (
            select(FaxPage)
            .options(joinedload(FaxPage.ocr_tokens))
            .where(
                FaxPage.fax_job_id == fax_job_id,
                FaxPage.page_number == page_number,
            )
        )
        return self.db.execute(stmt).unique().scalar_one_or_none()

    def get_first_page(self, fax_job_id: UUID) -> FaxPage | None:
        """Get the first page of a fax job."""
        stmt = (
            select(FaxPage)
            .where(
                FaxPage.fax_job_id == fax_job_id,
                FaxPage.page_number == 1,
            )
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_non_cover_pages(self, fax_job_id: UUID) -> list[FaxPage]:
        """Get all pages except cover pages."""
        stmt = (
            select(FaxPage)
            .where(
                FaxPage.fax_job_id == fax_job_id,
                FaxPage.is_cover_page == False,
            )
            .order_by(FaxPage.page_number)
        )
        return list(self.db.execute(stmt).scalars().all())

    def mark_as_cover_page(self, fax_page_id: UUID) -> FaxPage | None:
        """Mark a page as a cover page."""
        page = self.get_by_id(fax_page_id)
        if page:
            page.is_cover_page = True
            self.db.flush()
        return page

    def update_quality_metrics(
        self,
        fax_page_id: UUID,
        blur_score: float | None = None,
        skew_angle_deg: float | None = None,
        text_density: float | None = None,
    ) -> FaxPage | None:
        """
        Update page quality metrics.

        Args:
            fax_page_id: Page UUID.
            blur_score: Laplacian variance.
            skew_angle_deg: Detected skew angle.
            text_density: Text pixel ratio.

        Returns:
            Updated FaxPage or None.
        """
        page = self.get_by_id(fax_page_id)
        if not page:
            return None

        if blur_score is not None:
            page.blur_score = blur_score
        if skew_angle_deg is not None:
            page.skew_angle_deg = skew_angle_deg
        if text_density is not None:
            page.text_density = text_density

        self.db.flush()
        return page
