"""
Model version tracking — tracks ML model versions with promotion history.

Client requirement: "model_version table (model_type, version_tag, active, metrics_json, promoted_at)"
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from libs.shared.db.base import Base


class ModelVersion(Base):
    """
    Tracks ML model versions (OCR, VLM, LayoutLM) with promotion history.

    Only one version per model_type can be active at a time.
    """

    __tablename__ = "model_version"

    __table_args__ = (
        UniqueConstraint("model_type", "version_tag", name="uq_model_type_version"),
    )

    model_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("uuid_generate_v4()"),
    )

    model_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Model type: OCR, VLM, LAYOUTLM",
    )

    version_tag: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Version identifier e.g. 'layoutlm-v1.0'",
    )

    model_path: Mapped[str | None] = mapped_column(
        String(512),
        default=None,
        comment="Filesystem or HuggingFace model path",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        comment="Only one active per model_type",
    )

    metrics_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Accuracy, latency, field-level metrics",
    )

    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Model configuration",
    )

    promoted_at: Mapped[datetime | None] = mapped_column(
        default=None,
        comment="When this version was promoted to active",
    )

    promoted_by: Mapped[str | None] = mapped_column(
        String(255),
        default=None,
        comment="Who promoted this version",
    )

    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=text("NOW()"),
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        default=None,
        comment="Release notes / changelog",
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "model_version_id": str(self.model_version_id),
            "model_type": self.model_type,
            "version_tag": self.version_tag,
            "model_path": self.model_path,
            "is_active": self.is_active,
            "metrics_json": self.metrics_json,
            "config_json": self.config_json,
            "promoted_at": self.promoted_at.isoformat() if self.promoted_at else None,
            "promoted_by": self.promoted_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "notes": self.notes,
        }
