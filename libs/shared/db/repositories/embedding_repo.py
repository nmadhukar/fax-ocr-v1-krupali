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

    @staticmethod
    def _vector_to_str(embedding: list[float]) -> str:
        """Convert embedding to pgvector string, validating element types."""
        for i, v in enumerate(embedding):
            if not isinstance(v, (int, float)):
                raise TypeError(
                    f"Embedding element {i} is {type(v).__name__}, expected numeric"
                )
        return "[" + ",".join(str(v) for v in embedding) + "]"

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
        embedding_str = self._vector_to_str(embedding)

        result = self.db.execute(
            text("""
                INSERT INTO fax_embedding
                    (fax_job_id, fax_page_id, embedding, source_text,
                     source_type, chunk_index, token_count)
                VALUES
                    (:job_id, :page_id, CAST(:embedding AS vector), :source_text,
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

        params = []
        for row in rows:
            vec = row["embedding"]
            params.append({
                "fax_job_id": str(row["fax_job_id"]),
                "fax_page_id": str(row["fax_page_id"]) if row.get("fax_page_id") else None,
                "embedding_str": self._vector_to_str(vec),
                "source_text": row["source_text"],
                "source_type": row.get("source_type", "page_text"),
                "chunk_index": row.get("chunk_index", 0),
                "token_count": row.get("token_count"),
            })

        self.db.execute(
            text("""
                INSERT INTO fax_embedding
                    (fax_job_id, fax_page_id, embedding, source_text,
                     source_type, chunk_index, token_count)
                VALUES
                    (:fax_job_id, :fax_page_id, CAST(:embedding_str AS vector),
                     :source_text, :source_type, :chunk_index, :token_count)
            """),
            params,
        )
        return len(rows)

    def cosine_search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        fax_job_id: UUID | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find most similar embeddings using cosine distance.

        Args:
            query_embedding: 384-dim query vector.
            limit: Max results.
            fax_job_id: Optional filter by job.
            tenant_id: Required for cross-job searches to enforce tenant isolation.

        Returns:
            List of dicts with embedding_id, fax_job_id, fax_page_id,
            source_text, source_type, chunk_index, similarity_score.
        """
        vec_str = self._vector_to_str(query_embedding)

        if fax_job_id and tenant_id:
            sql = text("""
                SELECT
                    e.embedding_id, e.fax_job_id, e.fax_page_id,
                    e.source_text, e.source_type, e.chunk_index,
                    1 - (e.embedding <=> CAST(:query_vec AS vector)) AS similarity_score
                FROM fax_embedding e
                JOIN fax_job j ON j.fax_job_id = e.fax_job_id
                WHERE e.fax_job_id = :job_id
                  AND j.tenant_id = :tenant_id
                ORDER BY e.embedding <=> CAST(:query_vec AS vector)
                LIMIT :limit
            """)
            params = {"query_vec": vec_str, "job_id": str(fax_job_id), "tenant_id": tenant_id, "limit": limit}
        elif fax_job_id:
            raise ValueError(
                "cosine_search with fax_job_id also requires tenant_id "
                "to enforce tenant isolation"
            )
        elif tenant_id:
            sql = text("""
                SELECT
                    e.embedding_id, e.fax_job_id, e.fax_page_id,
                    e.source_text, e.source_type, e.chunk_index,
                    1 - (e.embedding <=> CAST(:query_vec AS vector)) AS similarity_score
                FROM fax_embedding e
                JOIN fax_job j ON j.fax_job_id = e.fax_job_id
                WHERE j.tenant_id = :tenant_id
                ORDER BY e.embedding <=> CAST(:query_vec AS vector)
                LIMIT :limit
            """)
            params = {"query_vec": vec_str, "tenant_id": tenant_id, "limit": limit}
        else:
            raise ValueError(
                "cosine_search requires either fax_job_id or tenant_id "
                "to enforce tenant isolation"
            )

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
