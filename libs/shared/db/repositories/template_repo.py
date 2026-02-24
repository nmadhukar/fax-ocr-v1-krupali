"""
Template repositories for template management.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, joinedload

from libs.shared.db.models.enums import DocTypeEnum, PayerNameEnum
from libs.shared.db.models.fax_template import (
    FaxTemplate,
    FaxTemplateField,
    FaxTemplateSample,
    FaxTemplateVersion,
)
from libs.shared.db.repositories.base import BaseRepository


class TemplateRepository(BaseRepository[FaxTemplate]):
    """Repository for FaxTemplate model."""

    def __init__(self, db: Session):
        super().__init__(db, FaxTemplate)

    def get_by_payer_and_type(
        self,
        payer_name: PayerNameEnum,
        doc_type: DocTypeEnum,
    ) -> list[FaxTemplate]:
        """
        Get templates for a payer and document type.

        Args:
            payer_name: Payer identifier.
            doc_type: Document type.

        Returns:
            List of active FaxTemplate instances.
        """
        stmt = (
            select(FaxTemplate)
            .where(
                FaxTemplate.payer_name == payer_name,
                FaxTemplate.doc_type == doc_type,
                FaxTemplate.is_active == True,
            )
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_active_templates(
        self,
        payer_name: PayerNameEnum | None = None,
    ) -> list[FaxTemplate]:
        """
        Get all active templates.

        Args:
            payer_name: Optional payer filter.

        Returns:
            List of active FaxTemplate instances.
        """
        conditions = [FaxTemplate.is_active == True]

        if payer_name:
            conditions.append(FaxTemplate.payer_name == payer_name)

        stmt = (
            select(FaxTemplate)
            .options(joinedload(FaxTemplate.versions))
            .where(and_(*conditions))
        )
        return list(self.db.execute(stmt).unique().scalars().all())

    def get_with_active_version(self, template_id: UUID) -> FaxTemplate | None:
        """Get template with its active version."""
        stmt = (
            select(FaxTemplate)
            .options(
                joinedload(FaxTemplate.versions).joinedload(FaxTemplateVersion.samples),
                joinedload(FaxTemplate.versions).joinedload(FaxTemplateVersion.fields),
            )
            .where(FaxTemplate.template_id == template_id)
        )
        return self.db.execute(stmt).unique().scalar_one_or_none()

    def list_templates(
        self,
        payer_name: PayerNameEnum | None = None,
        active_only: bool = False,
    ) -> list[FaxTemplate]:
        """List templates with optional payer filter and active-state filter."""
        conditions = []
        if payer_name is not None:
            conditions.append(FaxTemplate.payer_name == payer_name)
        if active_only:
            conditions.append(FaxTemplate.is_active == True)

        stmt = (
            select(FaxTemplate)
            .options(joinedload(FaxTemplate.versions))
            .order_by(FaxTemplate.created_at.desc())
        )
        if conditions:
            stmt = stmt.where(and_(*conditions))

        return list(self.db.execute(stmt).unique().scalars().all())


class TemplateVersionRepository(BaseRepository[FaxTemplateVersion]):
    """Repository for FaxTemplateVersion model."""

    def __init__(self, db: Session):
        super().__init__(db, FaxTemplateVersion)

    def get_active_version(self, template_id: UUID) -> FaxTemplateVersion | None:
        """Get the active version for a template."""
        stmt = (
            select(FaxTemplateVersion)
            .where(
                FaxTemplateVersion.template_id == template_id,
                FaxTemplateVersion.is_active == True,
            )
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_with_samples_and_fields(
        self,
        template_version_id: UUID,
    ) -> FaxTemplateVersion | None:
        """Get version with samples and fields."""
        stmt = (
            select(FaxTemplateVersion)
            .options(
                joinedload(FaxTemplateVersion.samples),
                joinedload(FaxTemplateVersion.fields),
            )
            .where(FaxTemplateVersion.template_version_id == template_version_id)
        )
        return self.db.execute(stmt).unique().scalar_one_or_none()

    def activate_version(self, template_version_id: UUID) -> FaxTemplateVersion | None:
        """
        Activate a version (deactivates others for same template).

        Args:
            template_version_id: Version to activate.

        Returns:
            Activated FaxTemplateVersion or None.
        """
        version = self.get_by_id(template_version_id)
        if not version:
            return None

        # Deactivate all versions for this template
        stmt = (
            select(FaxTemplateVersion)
            .where(FaxTemplateVersion.template_id == version.template_id)
        )
        all_versions = list(self.db.execute(stmt).scalars().all())

        for v in all_versions:
            v.is_active = False

        # Activate the requested version
        version.is_active = True
        version.activated_at = datetime.now(timezone.utc)

        self.db.flush()
        return version

    def get_all_active_versions(self) -> list[FaxTemplateVersion]:
        """Get all active versions across all templates."""
        stmt = (
            select(FaxTemplateVersion)
            .options(
                joinedload(FaxTemplateVersion.template),
                joinedload(FaxTemplateVersion.samples),
            )
            .where(FaxTemplateVersion.is_active == True)
        )
        return list(self.db.execute(stmt).unique().scalars().all())


class TemplateSampleRepository(BaseRepository[FaxTemplateSample]):
    """Repository for FaxTemplateSample model."""

    def __init__(self, db: Session):
        super().__init__(db, FaxTemplateSample)

    def get_by_version(self, template_version_id: UUID) -> list[FaxTemplateSample]:
        """Get all samples for a template version."""
        stmt = (
            select(FaxTemplateSample)
            .where(FaxTemplateSample.template_version_id == template_version_id)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_by_phash_range(
        self,
        query_phash: int,
        threshold: int = 10,
    ) -> list[FaxTemplateSample]:
        """
        Get samples within phash distance threshold.

        Note: This does a full scan. For production, consider
        using a specialized index or bloom filter.

        Args:
            query_phash: Query perceptual hash.
            threshold: Maximum Hamming distance.

        Returns:
            List of matching samples.
        """
        # For now, get all samples and filter in Python
        # In production, this should use a specialized index
        stmt = select(FaxTemplateSample)
        all_samples = list(self.db.execute(stmt).scalars().all())

        return [
            sample for sample in all_samples
            if self._hamming_distance(query_phash, sample.phash_value) <= threshold
        ]

    def get_all_with_version_info(self) -> list[tuple[FaxTemplateSample, FaxTemplateVersion]]:
        """Get all samples with their version info."""
        stmt = (
            select(FaxTemplateSample, FaxTemplateVersion)
            .join(FaxTemplateVersion)
            .where(FaxTemplateVersion.is_active == True)
        )
        return list(self.db.execute(stmt).all())

    @staticmethod
    def _hamming_distance(hash1: int, hash2: int) -> int:
        """Calculate Hamming distance between two hashes."""
        return bin(hash1 ^ hash2).count("1")


class TemplateFieldRepository(BaseRepository[FaxTemplateField]):
    """Repository for FaxTemplateField model."""

    def __init__(self, db: Session):
        super().__init__(db, FaxTemplateField)

    def get_by_version(
        self,
        template_version_id: UUID,
    ) -> list[FaxTemplateField]:
        """Get all fields for a template version."""
        stmt = (
            select(FaxTemplateField)
            .where(FaxTemplateField.template_version_id == template_version_id)
            .order_by(FaxTemplateField.field_key)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_required_fields(
        self,
        template_version_id: UUID,
    ) -> list[FaxTemplateField]:
        """Get required fields for a template version."""
        stmt = (
            select(FaxTemplateField)
            .where(
                FaxTemplateField.template_version_id == template_version_id,
                FaxTemplateField.is_required == True,
            )
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_field_by_key(
        self,
        template_version_id: UUID,
        field_key: str,
    ) -> FaxTemplateField | None:
        """Get a specific field by key."""
        stmt = (
            select(FaxTemplateField)
            .where(
                FaxTemplateField.template_version_id == template_version_id,
                FaxTemplateField.field_key == field_key,
            )
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_fields_for_page(
        self,
        template_version_id: UUID,
        page_number: int,
    ) -> list[FaxTemplateField]:
        """Get fields targeting a specific page."""
        stmt = (
            select(FaxTemplateField)
            .where(
                FaxTemplateField.template_version_id == template_version_id,
                FaxTemplateField.target_page == page_number,
            )
        )
        return list(self.db.execute(stmt).scalars().all())
