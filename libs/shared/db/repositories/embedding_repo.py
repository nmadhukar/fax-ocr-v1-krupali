"""
Repository for fax_embedding table.

Uses raw SQL for pgvector operations (INSERT with vector, cosine search).
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class EmbeddingRepository:
    """Repository for storing and querying embeddings via pgvector."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def insert_embedding(
        self,
        fax_job_id: UUID,
        fax_page_id: UUID | None,
        embedding: list[float],
        source_text: str,
        source_type: str = "page_text",
        chunk_index: int = 0,
        token_count: int | None = None,
    ) -> UUID:
        """Insert a single embedding row.

        Returns:
            The generated embedding_id.
        """
        embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

        result = self.db.execute(
            text("""
                INSERT INTO fax_embedding
                    (fax_job_id, fax_page_id, embedding, source_text,
                     source_type, chunk_index, token_count)
                VALUES
                    (:job_id, :page_id, :embedding::vector, :source_text,
                     :source_type, :chunk_index, :token_count)
                RETURNING embedding_id
            """),
            {
                "job_id": str(fax_job_id),
                "page_id": str(fax_page_id) if fax_page_id else None,
                "embedding": embedding_str,
                "source_text": source_text,
                "source_type": source_type,
                "chunk_index": chunk_index,
                "token_count": token_count,
            },
        )
        row = result.fetchone()
        return row[0]

    def bulk_insert_embeddings(
        self,
        rows: list[dict[str, Any]],
    ) -> int:
        """Bulk insert multiple embeddings.

        Each dict in `rows` must have keys:
            fax_job_id, fax_page_id, embedding (list[float]),
            source_text, source_type, chunk_index, token_count

        Returns:
            Number of rows inserted.
        """
        if not rows:
            return 0

        for row in rows:
            vec = row["embedding"]
            row["embedding_str"] = "[" + ",".join(str(v) for v in vec) + "]"

        self.db.execute(
            text("""
                INSERT INTO fax_embedding
                    (fax_job_id, fax_page_id, embedding, source_text,
                     source_type, chunk_index, token_count)
                VALUES
                    (:fax_job_id, :fax_page_id, :embedding_str::vector,
                     :source_text, :source_type, :chunk_index, :token_count)
            """),
            [
                {
                    "fax_job_id": str(r["fax_job_id"]),
                    "fax_page_id": str(r["fax_page_id"]) if r.get("fax_page_id") else None,
                    "embedding_str": r["embedding_str"],
                    "source_text": r["source_text"],
                    "source_type": r.get("source_type", "page_text"),
                    "chunk_index": r.get("chunk_index", 0),
                    "token_count": r.get("token_count"),
                }
                for r in rows
            ],
        )
        return len(rows)

    def cosine_search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        fax_job_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Find most similar embeddings using cosine distance.

        Args:
            query_embedding: 384-dim query vector.
            limit: Max results.
            fax_job_id: Optional filter by job.

        Returns:
            List of dicts with embedding_id, fax_job_id, fax_page_id,
            source_text, source_type, chunk_index, similarity_score.
        """
        vec_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

        if fax_job_id:
            sql = text("""
                SELECT
                    embedding_id, fax_job_id, fax_page_id,
                    source_text, source_type, chunk_index,
                    1 - (embedding <=> :query_vec::vector) AS similarity_score
                FROM fax_embedding
                WHERE fax_job_id = :job_id
                ORDER BY embedding <=> :query_vec::vector
                LIMIT :limit
            """)
            params = {"query_vec": vec_str, "job_id": str(fax_job_id), "limit": limit}
        else:
            sql = text("""
                SELECT
                    embedding_id, fax_job_id, fax_page_id,
                    source_text, source_type, chunk_index,
                    1 - (embedding <=> :query_vec::vector) AS similarity_score
                FROM fax_embedding
                ORDER BY embedding <=> :query_vec::vector
                LIMIT :limit
            """)
            params = {"query_vec": vec_str, "limit": limit}

        result = self.db.execute(sql, params)
        rows = result.fetchall()

        return [
            {
                "embedding_id": str(row[0]),
                "fax_job_id": str(row[1]),
                "fax_page_id": str(row[2]) if row[2] else None,
                "source_text": row[3],
                "source_type": row[4],
                "chunk_index": row[5],
                "similarity_score": float(row[6]),
            }
            for row in rows
        ]

    def count_by_job(self, fax_job_id: UUID) -> int:
        """Count embeddings for a specific job."""
        result = self.db.execute(
            text("SELECT COUNT(*) FROM fax_embedding WHERE fax_job_id = :job_id"),
            {"job_id": str(fax_job_id)},
        )
        return result.scalar() or 0
