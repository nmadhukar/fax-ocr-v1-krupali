-- Healthcare Fax Processing System - Initial Schema
-- Version: 1.0.0
-- Description: Core tables for fax processing pipeline

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ============================================
-- ENUM TYPES
-- ============================================

CREATE TYPE payer_name_enum AS ENUM (
    'ANTHEM', 'CARESOURCE', 'BUCKEYE', 'MOLINA',
    'UNITED_HEALTH', 'HUMANA', 'AMERIHEALTH', 'UNKNOWN'
);

CREATE TYPE doc_type_enum AS ENUM (
    'PRIOR_AUTH_FORM', 'HIPAA_RELEASE', 'CLINICAL_NOTES',
    'LAB_RESULTS', 'OTHER', 'UNKNOWN'
);

CREATE TYPE fax_job_status_enum AS ENUM (
    'PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'NEEDS_REVIEW'
);

CREATE TYPE extraction_method_enum AS ENUM (
    'TEMPLATE_OCR', 'DONUT', 'VLM', 'HUMAN', 'HYBRID'
);

-- ============================================
-- CORE TABLES
-- ============================================

-- Core entity tracking for fax jobs
CREATE TABLE fax_job (
    fax_job_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id VARCHAR(255) NOT NULL,
    original_filename VARCHAR(512) NOT NULL,
    file_storage_key VARCHAR(512) NOT NULL,
    file_sha256 VARCHAR(64) NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    total_pages INTEGER,

    payer_hint payer_name_enum DEFAULT 'UNKNOWN',
    doc_type doc_type_enum DEFAULT 'UNKNOWN',
    doc_type_conf NUMERIC(5,4),

    matched_template_version_id UUID,
    matched_template_score NUMERIC(5,4),

    status fax_job_status_enum DEFAULT 'PENDING',
    overall_conf NUMERIC(5,4),
    needs_review BOOLEAN DEFAULT false,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    processing_started_at TIMESTAMPTZ,
    processing_completed_at TIMESTAMPTZ,

    -- Audit fields
    external_fax_id VARCHAR(255),
    metadata JSONB DEFAULT '{}'::jsonb
);

COMMENT ON TABLE fax_job IS 'Core entity tracking for incoming fax documents';
COMMENT ON COLUMN fax_job.file_sha256 IS 'SHA-256 hash for deduplication and integrity';
COMMENT ON COLUMN fax_job.overall_conf IS 'Overall confidence score (0.0-1.0) for extraction';

-- Per-page storage with quality metrics
CREATE TABLE fax_page (
    fax_page_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    fax_job_id UUID NOT NULL REFERENCES fax_job(fax_job_id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    page_storage_key VARCHAR(512) NOT NULL,
    width_px INTEGER NOT NULL,
    height_px INTEGER NOT NULL,
    dpi INTEGER,

    -- Quality metrics
    blur_score NUMERIC(10,4),
    skew_angle_deg NUMERIC(6,3),
    text_density NUMERIC(5,4),
    is_cover_page BOOLEAN DEFAULT false,

    -- Preprocessing metadata
    preprocessed_storage_key VARCHAR(512),
    preprocessing_applied JSONB DEFAULT '[]'::jsonb,

    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(fax_job_id, page_number)
);

COMMENT ON TABLE fax_page IS 'Individual pages extracted from fax documents';
COMMENT ON COLUMN fax_page.blur_score IS 'Laplacian variance score - higher is sharper';
COMMENT ON COLUMN fax_page.text_density IS 'Ratio of text pixels to total pixels (0.0-1.0)';

-- OCR evidence layer - stores all extracted tokens
CREATE TABLE fax_ocr_token (
    ocr_token_id BIGSERIAL PRIMARY KEY,
    fax_page_id UUID NOT NULL REFERENCES fax_page(fax_page_id) ON DELETE CASCADE,
    token_text TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    word_number INTEGER NOT NULL,

    -- Normalized bounding box [0.0 - 1.0]
    bbox_x0 NUMERIC(8,6) NOT NULL,
    bbox_y0 NUMERIC(8,6) NOT NULL,
    bbox_x1 NUMERIC(8,6) NOT NULL,
    bbox_y1 NUMERIC(8,6) NOT NULL,

    confidence NUMERIC(5,4) NOT NULL,
    is_numeric BOOLEAN DEFAULT false,
    is_date_like BOOLEAN DEFAULT false,

    -- Token metadata
    font_size_estimate INTEGER,
    is_bold BOOLEAN DEFAULT false,
    is_handwritten BOOLEAN DEFAULT false
);

COMMENT ON TABLE fax_ocr_token IS 'OCR tokens with normalized bounding boxes - critical for evidence linking';
COMMENT ON COLUMN fax_ocr_token.bbox_x0 IS 'Normalized X coordinate (0.0-1.0) of bbox left edge';

-- ============================================
-- TEMPLATE TABLES
-- ============================================

-- Template definitions (parent entity)
CREATE TABLE fax_template (
    template_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    payer_name payer_name_enum NOT NULL,
    doc_type doc_type_enum NOT NULL,
    template_name VARCHAR(255) NOT NULL,
    description TEXT,
    is_active BOOLEAN DEFAULT true,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    created_by VARCHAR(255),

    UNIQUE(payer_name, doc_type, template_name)
);

COMMENT ON TABLE fax_template IS 'Template definitions for payer-specific document formats';

-- Template versions for A/B testing and rollback
CREATE TABLE fax_template_version (
    template_version_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    template_id UUID NOT NULL REFERENCES fax_template(template_id) ON DELETE CASCADE,
    version_label VARCHAR(50) NOT NULL,

    -- Matching thresholds
    match_min_score NUMERIC(5,4) DEFAULT 0.75,
    match_phash_threshold INTEGER DEFAULT 10,
    match_orb_min_matches INTEGER DEFAULT 20,

    is_active BOOLEAN DEFAULT false,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    activated_at TIMESTAMPTZ,

    UNIQUE(template_id, version_label)
);

COMMENT ON TABLE fax_template_version IS 'Versioned template configurations for safe updates';

-- Template sample images for matching
CREATE TABLE fax_template_sample (
    sample_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    template_version_id UUID NOT NULL REFERENCES fax_template_version(template_version_id) ON DELETE CASCADE,
    sample_storage_key VARCHAR(512) NOT NULL,

    -- Matching features
    phash_value BIGINT NOT NULL,
    orb_descriptors BYTEA,
    orb_keypoints BYTEA,

    width_px INTEGER NOT NULL,
    height_px INTEGER NOT NULL,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE fax_template_sample IS 'Sample images with precomputed matching features';
COMMENT ON COLUMN fax_template_sample.phash_value IS 'Perceptual hash for fast prefiltering';
COMMENT ON COLUMN fax_template_sample.orb_descriptors IS 'Serialized ORB feature descriptors';

-- Template field definitions (ROI extraction)
CREATE TABLE fax_template_field (
    template_field_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    template_version_id UUID NOT NULL REFERENCES fax_template_version(template_version_id) ON DELETE CASCADE,
    field_key VARCHAR(100) NOT NULL,
    field_label VARCHAR(255),
    is_required BOOLEAN DEFAULT false,

    -- Normalized ROI [0.0 - 1.0]
    roi_x0 NUMERIC(8,6) NOT NULL,
    roi_y0 NUMERIC(8,6) NOT NULL,
    roi_x1 NUMERIC(8,6) NOT NULL,
    roi_y1 NUMERIC(8,6) NOT NULL,

    -- Optional: target page (default first page)
    target_page INTEGER DEFAULT 1,

    -- Validation
    validation_regex TEXT,
    validation_message TEXT,

    -- Extraction hints
    expected_type VARCHAR(50) DEFAULT 'text',  -- text, date, number, phone, etc.
    post_processing JSONB DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(template_version_id, field_key)
);

COMMENT ON TABLE fax_template_field IS 'Field definitions with ROI coordinates for template extraction';

-- ============================================
-- EXTRACTION TABLES
-- ============================================

-- Extracted fields with evidence
CREATE TABLE fax_extracted_field (
    extracted_field_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    fax_job_id UUID NOT NULL REFERENCES fax_job(fax_job_id) ON DELETE CASCADE,
    field_key VARCHAR(100) NOT NULL,
    field_value TEXT,

    method extraction_method_enum NOT NULL,
    field_conf NUMERIC(5,4),

    -- Evidence linking
    evidence_bbox JSONB,  -- {page: 1, x0: 0.1, y0: 0.2, x1: 0.4, y1: 0.25}
    evidence_text TEXT,
    evidence_token_ids BIGINT[],

    -- Multiple extraction candidates
    candidates JSONB DEFAULT '[]'::jsonb,

    -- Validation result
    validation_passed BOOLEAN,
    validation_errors TEXT[],

    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(fax_job_id, field_key, method)
);

COMMENT ON TABLE fax_extracted_field IS 'Extracted field values with method attribution and evidence';

-- Final extraction output
CREATE TABLE fax_extraction (
    extraction_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    fax_job_id UUID NOT NULL REFERENCES fax_job(fax_job_id) ON DELETE CASCADE,

    extraction_json JSONB NOT NULL,

    -- Version tracking
    model_versions JSONB,  -- {ocr: "PP-OCRv5", vlm: "donut-base", ...}
    pipeline_version VARCHAR(50),

    -- Quality metrics
    total_fields INTEGER,
    high_conf_fields INTEGER,
    low_conf_fields INTEGER,
    missing_fields INTEGER,

    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(fax_job_id)
);

COMMENT ON TABLE fax_extraction IS 'Final merged extraction result with version metadata';

-- ============================================
-- REVIEW WORKFLOW TABLES
-- ============================================

-- Review queue and workflow
CREATE TABLE fax_review (
    review_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    fax_job_id UUID NOT NULL REFERENCES fax_job(fax_job_id) ON DELETE CASCADE,

    review_packet JSONB NOT NULL,
    review_reasons TEXT[],
    priority INTEGER DEFAULT 0,

    -- Claim workflow
    claimed_by VARCHAR(255),
    claimed_at TIMESTAMPTZ,
    claim_expires_at TIMESTAMPTZ,

    -- Submission
    submitted_by VARCHAR(255),
    submitted_at TIMESTAMPTZ,
    corrections JSONB,

    -- Metrics
    time_to_claim_seconds INTEGER,
    time_to_submit_seconds INTEGER,

    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(fax_job_id)
);

COMMENT ON TABLE fax_review IS 'Human review workflow for uncertain extractions';

-- Feedback for training
CREATE TABLE fax_feedback (
    feedback_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    fax_job_id UUID NOT NULL REFERENCES fax_job(fax_job_id) ON DELETE CASCADE,
    review_id UUID REFERENCES fax_review(review_id),

    field_key VARCHAR(100) NOT NULL,
    original_value TEXT,
    corrected_value TEXT,
    original_bbox JSONB,
    corrected_bbox JSONB,

    feedback_type VARCHAR(50),  -- 'correction', 'confirmation', 'rejection'
    feedback_source VARCHAR(50),  -- 'human_review', 'api_correction', 'auto_validation'

    created_at TIMESTAMPTZ DEFAULT NOW(),
    created_by VARCHAR(255)
);

COMMENT ON TABLE fax_feedback IS 'Feedback data for model improvement and training';

-- ============================================
-- AUDIT TABLES
-- ============================================

-- HIPAA-compliant audit log
CREATE TABLE audit_log (
    audit_id BIGSERIAL PRIMARY KEY,

    tenant_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255),
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(100) NOT NULL,
    resource_id UUID,

    details JSONB DEFAULT '{}'::jsonb,
    ip_address INET,
    user_agent TEXT,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE audit_log IS 'HIPAA-compliant audit trail for all data access';
