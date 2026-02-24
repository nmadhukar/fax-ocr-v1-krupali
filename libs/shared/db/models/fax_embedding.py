"""
Embedding model - Vector embeddings for semantic document search.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.shared.db.base import Base

if TYPE_CHECKING:
    from libs.shared.db.models.fax_job import FaxJob
    from libs.shared.db.models.fax_page import FaxPage


class FaxEmbedding(Base):
    """
    Vector embedding for semantic search over OCR text.

    Each row stores one chunk of embedded text (384 dimensions from
    sentence-transformers all-MiniLM-L6-v2).  The `embedding` column
    is stored using pgvector but mapped here as a generic column since
    SQLAlchemy handles it via raw SQL / pgvector ops.
    """

    __tablename__ = "fax_embedding"

    # Primary key
    embedding_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("uuid_generate_v4()"),
    )

    # Foreign keys
    fax_job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fax_job.fax_job_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fax_page_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fax_page.fax_page_id", ondelete="CASCADE"),
        nullable=True,
    )

    # Source text that was embedded
    source_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="page_text | field_value | summary",
    )

    # Chunk metadata
    chunk_index: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )
    token_count: Mapped[int | None] = mapped_column(
        Integer,
        default=None,
    )

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=text("NOW()"),
    )

    # NOTE: The `embedding vector(384)` column is managed via raw SQL
    # (pgvector extension).  We don't map it as a regular SQLAlchemy
    # column because pgvector types require the pgvector SQLAlchemy
    # extension.  Instead, inserts and queries use text() SQL.
