-- Healthcare Fax Processing System - Vector Search Support
-- Version: 1.0.0
-- Description: pgvector tables for semantic search (Phase 2)

-- Ensure vector extension is enabled
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================
-- EMBEDDING TABLES (Phase 2)
-- ============================================

-- Document embeddings for semantic search
CREATE TABLE fax_embedding (
    embedding_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    fax_job_id UUID NOT NULL REFERENCES fax_job(fax_job_id) ON DELETE CASCADE,
    fax_page_id UUID REFERENCES fax_page(fax_page_id) ON DELETE CASCADE,

    -- Embedding vector (all-MiniLM-L6-v2 = 384 dimensions)
    embedding vector(384) NOT NULL,

    -- Source text that was embedded
    source_text TEXT NOT NULL,
    source_type VARCHAR(50) NOT NULL,  -- 'page_text', 'field_value', 'summary'

    -- Metadata
    chunk_index INTEGER DEFAULT 0,
    token_count INTEGER,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE fax_embedding IS 'Vector embeddings for semantic document search';

-- Vector similarity search index (IVFFlat for large scale)
CREATE INDEX idx_fax_embedding_vector ON fax_embedding
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Lookup by job
CREATE INDEX idx_fax_embedding_job ON fax_embedding(fax_job_id);

-- Lookup by type
CREATE INDEX idx_fax_embedding_type ON fax_embedding(source_type);

-- ============================================
-- CITATION TABLES (Phase 2)
-- ============================================

-- Citation evidence for Query API responses
CREATE TABLE query_citation (
    citation_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    query_id UUID NOT NULL,

    fax_job_id UUID NOT NULL REFERENCES fax_job(fax_job_id) ON DELETE CASCADE,
    fax_page_id UUID REFERENCES fax_page(fax_page_id) ON DELETE CASCADE,

    -- Citation details
    cited_text TEXT NOT NULL,
    relevance_score NUMERIC(5,4),

    -- Bounding box for highlighting
    bbox JSONB,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE query_citation IS 'Citation evidence linking query results to source documents';

CREATE INDEX idx_query_citation_query ON query_citation(query_id);
CREATE INDEX idx_query_citation_job ON query_citation(fax_job_id);
