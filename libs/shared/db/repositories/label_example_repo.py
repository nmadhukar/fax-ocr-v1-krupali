"""
Repository for fax_label_example training data.

Provides CRUD operations and training data export for LayoutLM fine-tuning.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from libs.shared.db.models.enums import DocTypeEnum, PayerNameEnum
from libs.shared.db.models.fax_label_example import FaxLabelExample
from libs.shared.db.repositories.base import BaseRepository


class LabelExampleRepository(BaseRepository[FaxLabelExample]):
    """Repository for ground-truth training data."""

    def __init__(self, db: Session):
        super().__init__(db, FaxLabelExample)

    def create_label(
        self,
        fax_job_id: UUID,
        fax_page_id: UUID,
        field_key: str,
        ground_truth_value: str,
        page_storage_key: str,
        ground_truth_bbox: dict | None = None,
        payer_name: PayerNameEnum | None = None,
        doc_type: DocTypeEnum | None = None,
        source: str = "human_review",
        created_by: str | None = None,
    ) -> FaxLabelExample:
        """Create a new training label example."""
        label = FaxLabelExample(
            fax_job_id=fax_job_id,
            fax_page_id=fax_page_id,
            field_key=field_key,
            ground_truth_value=ground_truth_value,
            page_storage_key=page_storage_key,
            ground_truth_bbox=ground_truth_bbox,
            payer_name=payer_name,
            doc_type=doc_type,
            source=source,
            is_verified=True,
            created_by=created_by,
        )
        self.db.add(label)
        self.db.flush()
        return label

    def get_training_data(
        self,
        payer_name: PayerNameEnum | None = None,
        field_key: str | None = None,
        verified_only: bool = True,
        limit: int = 10000,
    ) -> list[FaxLabelExample]:
        """
        Get training data with optional filters.

        Args:
            payer_name: Filter by payer.
            field_key: Filter by field key.
            verified_only: Only return verified labels.
            limit: Maximum results.

        Returns:
            List of FaxLabelExample records.
        """
        stmt = select(FaxLabelExample)

        if payer_name:
            stmt = stmt.where(FaxLabelExample.payer_name == payer_name)
        if field_key:
            stmt = stmt.where(FaxLabelExample.field_key == field_key)
        if verified_only:
            stmt = stmt.where(FaxLabelExample.is_verified.is_(True))

        stmt = stmt.order_by(FaxLabelExample.created_at.desc()).limit(limit)

        return list(self.db.execute(stmt).scalars().all())

    def export_for_training(
        self,
        payer_name: PayerNameEnum | None = None,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        """
        Export training data for LayoutLM fine-tuning.

        Returns list of dicts with:
          - page_storage_key: MinIO key for the page image
          - ground_truth: {gt_parse: {field_key: value, ...}}
          - payer_name, doc_type

        Groups by page to consolidate fields per page image.
        """
        labels = self.get_training_data(payer_name=payer_name, limit=limit)

        # Group by page
        page_groups: dict[str, list[FaxLabelExample]] = {}
        for label in labels:
            page_groups.setdefault(label.page_storage_key, []).append(label)

        result = []
        for page_key, page_labels in page_groups.items():
            gt_parse = {}
            for label in page_labels:
                gt_parse[label.field_key] = label.ground_truth_value

            result.append({
                "page_storage_key": page_key,
                "ground_truth": {"gt_parse": gt_parse},
                "payer_name": page_labels[0].payer_name.value if page_labels[0].payer_name else None,
                "doc_type": page_labels[0].doc_type.value if page_labels[0].doc_type else None,
            })

        return result

    def count_by_payer(self) -> dict[str, int]:
        """Get count of training examples per payer."""
        from sqlalchemy import func

        stmt = (
            select(FaxLabelExample.payer_name, func.count())
            .where(FaxLabelExample.is_verified.is_(True))
            .group_by(FaxLabelExample.payer_name)
        )
        rows = self.db.execute(stmt).all()
        return {
            (r[0].value if r[0] else "UNKNOWN"): r[1]
            for r in rows
        }
