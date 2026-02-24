"""
FaxLabelExample model - Ground-truth training data from human review.

Each row represents a corrected field value from human review,
linked to the source page image. Used for LayoutLM fine-tuning.
"""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Enum, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.shared.db.base import Base
from libs.shared.db.models.enums import DocTypeEnum, PayerNameEnum

if TYPE_CHECKING:
    from libs.shared.db.models.fax_job import FaxJob
    from libs.shared.db.models.fax_page import FaxPage


class FaxLabelExample(Base):
    """
    Ground-truth training data from human review corrections.

    Each record links a corrected field value to its source page image,
    enabling LayoutLM fine-tuning with verified labels.
    """

    __tablename__ = "fax_label_example"

    label_example_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("uuid_generate_v4()"),
    )

    fax_job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fax_job.fax_job_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    fax_page_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fax_page.fax_page_id", ondelete="CASCADE"),
        nullable=False,
    )

    field_key: Mapped[str] = mapped_column(String(100), nullable=False)
    ground_truth_value: Mapped[str] = mapped_column(Text, nullable=False)
    ground_truth_bbox: Mapped[dict | None] = mapped_column(JSONB, default=None)

    payer_name: Mapped[PayerNameEnum | None] = mapped_column(
        Enum(PayerNameEnum, name="payer_name_enum", create_type=False),
        nullable=True,
    )
    doc_type: Mapped[DocTypeEnum | None] = mapped_column(
        Enum(DocTypeEnum, name="doc_type_enum", create_type=False),
        nullable=True,
    )

    page_storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="human_review")
    is_verified: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow,
        server_default=text("NOW()"),
    )
    created_by: Mapped[str | None] = mapped_column(String(255), default=None)

    # Relationships
    fax_job: Mapped["FaxJob"] = relationship("FaxJob")
    fax_page: Mapped["FaxPage"] = relationship("FaxPage")
