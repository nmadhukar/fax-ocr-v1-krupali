"""
Query endpoint - structured field lookup (Tier 1), semantic search (Tier 2),
and summary synthesis (Tier 3).

Tier 1: SQL lookup in fax_extracted_field + fax_extraction
Tier 2: pgvector cosine similarity search over fax_embedding
Tier 3: deterministic summary from Tier 1/2 evidence
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from libs.shared.config import get_settings
from libs.shared.db.session import get_db
from libs.shared.security.auth import AuthUser, get_current_user
from libs.shared.security.rate_limiter import get_api_rate_limiter

logger = logging.getLogger(__name__)

router = APIRouter()


# -----------------------------------------------------------------------
# Request / Response schemas
# -----------------------------------------------------------------------

class QueryRequest(BaseModel):
    """Query request body."""

    query: str = Field(..., min_length=1, max_length=2000, description="Search query text")
    tier: int = Field(default=1, ge=1, le=3, description="Query tier: 1=structured, 2=semantic, 3=summary")
    limit: int = Field(default=10, ge=1, le=100, description="Max results to return")
    fax_job_id: str | None = Field(default=None, description="Optional: limit to specific fax job")

    @field_validator("fax_job_id")
    @classmethod
    def validate_fax_job_id(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                UUID(v)
            except ValueError as exc:
                raise ValueError("fax_job_id must be a valid UUID") from exc
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

    params: dict[str, Any] = {
        "query_key": query_lower,
        "query_like": f"%{escaped}%",
        "limit": limit,
        "tenant_id": tenant_id,
    }

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
                value=(r["source_text"] or "")[:500],
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
# Tier 3: Deterministic summary synthesis
# -----------------------------------------------------------------------

def _tier3_search(
    db: Session,
    query: str,
    limit: int,
    tenant_id: str,
    fax_job_id: str | None = None,
) -> list[QueryResultItem]:
    """Build an evidence-backed summary from Tier 1 and Tier 2 results."""
    settings = get_settings()

    tier1_results = _tier1_search(
        db=db,
        query=query,
        limit=max(5, min(limit, 25)),
        tenant_id=tenant_id,
        fax_job_id=fax_job_id,
    )

    tier2_results: list[QueryResultItem] = []
    if settings.features.vector_search:
        tier2_results = _tier2_search(
            db=db,
            query=query,
            limit=min(limit, 10),
            tenant_id=tenant_id,
            fax_job_id=fax_job_id,
        )

    field_fragments: list[str] = []
    seen_fields: set[tuple[str, str]] = set()
    for item in tier1_results:
        key = (item.field_key or "").strip()
        value = (item.value or "").strip()
        if not key or not value:
            continue
        dedupe_key = (key.lower(), value.lower())
        if dedupe_key in seen_fields:
            continue
        seen_fields.add(dedupe_key)
        field_fragments.append(f"{key}: {value}")
        if len(field_fragments) >= 6:
            break

    semantic_fragments: list[str] = []
    seen_semantic: set[str] = set()
    for item in tier2_results:
        value = (item.value or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen_semantic:
            continue
        seen_semantic.add(key)
        semantic_fragments.append(value[:140])
        if len(semantic_fragments) >= 3:
            break

    summary_parts: list[str] = []
    if field_fragments:
        summary_parts.append(f"Matched extracted fields: {'; '.join(field_fragments)}.")
    if semantic_fragments:
        summary_parts.append(f"Closest semantic evidence: {' | '.join(semantic_fragments)}.")
    if not summary_parts:
        summary_parts.append("No matching records found for this query and filter set.")

    anchor_job_id = (
        fax_job_id
        or (tier1_results[0].fax_job_id if tier1_results else None)
        or (tier2_results[0].fax_job_id if tier2_results else None)
        or "summary"
    )

    summary_item = QueryResultItem(
        fax_job_id=anchor_job_id,
        value=" ".join(summary_parts),
        method="SUMMARY_SYNTHESIS",
        source_type="summary",
    )

    supporting_items = tier1_results[: max(0, limit - 1)]
    return [summary_item, *supporting_items][:limit]


# -----------------------------------------------------------------------
# Endpoint
# -----------------------------------------------------------------------

@router.post("/query", response_model=QueryResponse)
def query_faxes(
    request: Request,
    body: QueryRequest,
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QueryResponse:
    """Query extracted fax data by tier."""
    get_api_rate_limiter().check(request)

    start = datetime.now(timezone.utc)
    tenant_id = user.tenant_id
    query_id = str(uuid4())

    results: list[QueryResultItem] = []

    if body.tier == 1:
        results = _tier1_search(
            db, body.query, body.limit, tenant_id, body.fax_job_id,
        )
    elif body.tier == 2:
        settings = get_settings()
        if not settings.features.vector_search:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tier 2 (vector search) is disabled via feature flag",
            )
        results = _tier2_search(
            db, body.query, body.limit, tenant_id, body.fax_job_id,
        )
    elif body.tier == 3:
        results = _tier3_search(
            db, body.query, body.limit, tenant_id, body.fax_job_id,
        )

    # Store citations for Tier 2
    if body.tier == 2 and results:
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
        query=body.query,
        tier=body.tier,
        results=results,
        total_results=len(results),
        latency_ms=round(elapsed, 1),
    )
