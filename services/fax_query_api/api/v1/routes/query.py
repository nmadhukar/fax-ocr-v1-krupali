"""
Query endpoint — structured field lookup (Tier 1) and semantic search (Tier 2).

Tier 1: SQL lookup in fax_extracted_field + fax_extraction
Tier 2: pgvector cosine similarity search over fax_embedding
Tier 3: Summariser (OFF by default — stub)
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from libs.shared.config import get_settings
from libs.shared.db.session import get_db
from libs.shared.security.auth import AuthUser, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


# -----------------------------------------------------------------------
# Request / Response schemas
# -----------------------------------------------------------------------

class QueryRequest(BaseModel):
    """Query request body."""

    query: str = Field(..., min_length=1, max_length=2000, description="Search query text")
    tier: int = Field(default=1, ge=1, le=3, description="Query tier: 1=structured, 2=semantic, 3=summariser")
    limit: int = Field(default=10, ge=1, le=100, description="Max results to return")
    fax_job_id: str | None = Field(default=None, description="Optional: limit to specific fax job")

    @field_validator("fax_job_id")
    @classmethod
    def validate_fax_job_id(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                UUID(v)
            except ValueError:
                raise ValueError("fax_job_id must be a valid UUID")
        return v


class QueryResultItem(BaseModel):
    """Single query result."""

    fax_job_id: str
    fax_page_id: str | None = None
    field_key: str | None = None
    value: str
    confidence: float | None = None
    method: str | None = None
    source_type: str = "field"
    similarity_score: float | None = None


class QueryResponse(BaseModel):
    """Query response."""

    query_id: str
    query: str
    tier: int
    results: list[QueryResultItem]
    total_results: int
    latency_ms: float


# -----------------------------------------------------------------------
# Tier 1: Structured field lookup
# -----------------------------------------------------------------------

def _tier1_search(
    db: Session,
    query: str,
    limit: int,
    tenant_id: str,
    fax_job_id: str | None = None,
) -> list[QueryResultItem]:
    """Search extracted fields by field_key or value match."""
    query_lower = query.lower().strip()

    # Escape LIKE special characters to prevent pattern injection
    escaped = query_lower.replace("%", r"\%").replace("_", r"\_")

    # Search by field key exact match or value ILIKE
    params: dict[str, Any] = {
        "query_key": query_lower,
        "query_like": f"%{escaped}%",
        "limit": limit,
        "tenant_id": tenant_id,
    }

    # Build SQL without f-string interpolation — use static conditional blocks
    if fax_job_id:
        params["job_id"] = fax_job_id
        sql = text("""
            SELECT
                f.fax_job_id::text,
                f.field_key,
                f.field_value,
                f.field_conf,
                f.method::text
            FROM fax_extracted_field f
            JOIN fax_job j ON j.fax_job_id = f.fax_job_id
            WHERE (
                LOWER(f.field_key) = :query_key
                OR LOWER(f.field_value) LIKE :query_like
            )
            AND j.tenant_id = :tenant_id
            AND f.fax_job_id = :job_id
            ORDER BY f.field_conf DESC NULLS LAST
            LIMIT :limit
        """)
    else:
        sql = text("""
            SELECT
                f.fax_job_id::text,
                f.field_key,
                f.field_value,
                f.field_conf,
                f.method::text
            FROM fax_extracted_field f
            JOIN fax_job j ON j.fax_job_id = f.fax_job_id
            WHERE (
                LOWER(f.field_key) = :query_key
                OR LOWER(f.field_value) LIKE :query_like
            )
            AND j.tenant_id = :tenant_id
            ORDER BY f.field_conf DESC NULLS LAST
            LIMIT :limit
        """)

    result = db.execute(sql, params)
    rows = result.fetchall()

    return [
        QueryResultItem(
            fax_job_id=row[0],
            field_key=row[1],
            value=row[2] or "",
            confidence=float(row[3]) if row[3] else None,
            method=row[4],
            source_type="field",
        )
        for row in rows
    ]


# -----------------------------------------------------------------------
# Tier 2: Semantic vector search
# -----------------------------------------------------------------------

def _tier2_search(
    db: Session,
    query: str,
    limit: int,
    tenant_id: str,
    fax_job_id: str | None = None,
) -> list[QueryResultItem]:
    """Semantic search using pgvector cosine similarity."""
    try:
        from libs.shared.embeddings import get_embedding_client
        from libs.shared.db.repositories.embedding_repo import EmbeddingRepository

        emb_client = get_embedding_client()
        if not emb_client.is_available():
            logger.warning("Embedding model not available for Tier 2 search")
            return []

        # Encode the query
        query_vec = emb_client.encode_single(query)
        if not query_vec:
            return []

        emb_repo = EmbeddingRepository(db)
        job_uuid = UUID(fax_job_id) if fax_job_id else None
        results = emb_repo.cosine_search(
            query_embedding=query_vec,
            limit=limit,
            tenant_id=tenant_id,
            fax_job_id=job_uuid,
        )

        return [
            QueryResultItem(
                fax_job_id=r["fax_job_id"],
                fax_page_id=r["fax_page_id"],
                value=r["source_text"],
                source_type=r["source_type"],
                similarity_score=r["similarity_score"],
            )
            for r in results
        ]

    except ImportError:
        logger.warning("sentence-transformers not installed; Tier 2 unavailable")
        return []
    except Exception:
        logger.warning("Tier 2 semantic search failed", exc_info=True)
        return []


# -----------------------------------------------------------------------
# Endpoint
# -----------------------------------------------------------------------

@router.post("/query", response_model=QueryResponse)
def query_faxes(
    request: QueryRequest,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QueryResponse:
    """Query extracted fax data.

    - **Tier 1**: Structured SQL lookup by field key or value
    - **Tier 2**: Semantic vector search (pgvector cosine similarity)
    - **Tier 3**: Summariser (OFF — returns empty)
    """
    start = datetime.now(timezone.utc)
    tenant_id = user.tenant_id
    query_id = str(uuid4())

    results: list[QueryResultItem] = []

    if request.tier == 1:
        results = _tier1_search(
            db, request.query, request.limit, tenant_id, request.fax_job_id,
        )
    elif request.tier == 2:
        settings = get_settings()
        if not settings.features.vector_search:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tier 2 (vector search) is disabled via feature flag",
            )
        results = _tier2_search(
            db, request.query, request.limit, tenant_id, request.fax_job_id,
        )
    elif request.tier == 3:
        # Tier 3: Summariser stub — OFF by default
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Tier 3 (summariser) is not yet implemented",
        )

    # Store citations for Tier 2
    if request.tier == 2 and results:
        try:
            for r in results:
                db.execute(
                    text("""
                        INSERT INTO query_citation
                            (query_id, fax_job_id, fax_page_id,
                             cited_text, relevance_score)
                        VALUES
                            (:query_id, :job_id::uuid, :page_id::uuid,
                             :cited_text, :score)
                    """),
                    {
                        "query_id": query_id,
                        "job_id": r.fax_job_id,
                        "page_id": r.fax_page_id,
                        "cited_text": r.value[:500],
                        "score": r.similarity_score,
                    },
                )
            db.commit()
        except Exception:
            db.rollback()
            logger.warning("Failed to store query citations", exc_info=True)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000

    return QueryResponse(
        query_id=query_id,
        query=request.query,
        tier=request.tier,
        results=results,
        total_results=len(results),
        latency_ms=round(elapsed, 1),
    )
