"""
Repository for model version tracking.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from libs.shared.db.models.model_version import ModelVersion
from libs.shared.db.repositories.base import BaseRepository


class ModelVersionRepository(BaseRepository[ModelVersion]):
    """Repository for ModelVersion CRUD + promotion."""

    def __init__(self, db: Session):
        super().__init__(db, ModelVersion)

    def get_active(self, model_type: str) -> ModelVersion | None:
        """Get the currently active version for a model type."""
        stmt = (
            select(ModelVersion)
            .where(
                ModelVersion.model_type == model_type,
                ModelVersion.is_active == True,
            )
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_type(self, model_type: str) -> list[ModelVersion]:
        """Get all versions for a model type, newest first."""
        stmt = (
            select(ModelVersion)
            .where(ModelVersion.model_type == model_type)
            .order_by(ModelVersion.created_at.desc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def promote(
        self,
        model_version_id: UUID,
        promoted_by: str = "system",
    ) -> ModelVersion | None:
        """
        Promote a version to active (deactivates others of same type).

        Args:
            model_version_id: Version to promote.
            promoted_by: Who is promoting.

        Returns:
            Promoted ModelVersion or None if not found.
        """
        version = self.get_by_id(model_version_id)
        if not version:
            return None

        # Deactivate all versions of same type
        siblings = self.get_by_type(version.model_type)
        for sibling in siblings:
            sibling.is_active = False

        # Activate this version
        version.is_active = True
        version.promoted_at = datetime.now(timezone.utc)
        version.promoted_by = promoted_by

        self.db.flush()
        return version

    def register_version(
        self,
        model_type: str,
        version_tag: str,
        model_path: str | None = None,
        notes: str | None = None,
        config: dict | None = None,
    ) -> ModelVersion:
        """Register a new model version (inactive by default)."""
        version = ModelVersion(
            model_type=model_type,
            version_tag=version_tag,
            model_path=model_path,
            notes=notes,
            config_json=config or {},
        )
        self.create(version)
        return version

    def update_metrics(
        self,
        model_version_id: UUID,
        metrics: dict,
    ) -> ModelVersion | None:
        """Update metrics for a model version."""
        version = self.get_by_id(model_version_id)
        if not version:
            return None
        version.metrics_json = metrics
        self.db.flush()
        return version
