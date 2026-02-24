-- Healthcare Fax Processing System - Indexes
-- Version: 1.0.0
-- Description: Performance indexes for critical queries

-- ============================================
-- FAX_JOB INDEXES
-- ============================================

-- Tenant isolation and status filtering
CREATE INDEX idx_fax_job_tenant_status ON fax_job(tenant_id, status);
CREATE INDEX idx_fax_job_tenant_created ON fax_job(tenant_id, created_at DESC);

-- Deduplication lookup
CREATE INDEX idx_fax_job_sha256 ON fax_job(file_sha256);

-- Review queue queries
CREATE INDEX idx_fax_job_needs_review ON fax_job(needs_review, created_at DESC) WHERE needs_review = true;

-- Template matching queries
CREATE INDEX idx_fax_job_template ON fax_job(matched_template_version_id) WHERE matched_template_version_id IS NOT NULL;

-- ============================================
-- FAX_PAGE INDEXES
-- ============================================

-- Page lookup by job
CREATE INDEX idx_fax_page_job ON fax_page(fax_job_id, page_number);

-- Cover page detection
CREATE INDEX idx_fax_page_cover ON fax_page(fax_job_id) WHERE is_cover_page = true;

-- ============================================
-- FAX_OCR_TOKEN INDEXES (Critical - millions of rows)
-- ============================================

-- Token lookup by page
CREATE INDEX idx_ocr_token_page ON fax_ocr_token(fax_page_id);

-- Spatial queries for ROI extraction
CREATE INDEX idx_ocr_token_bbox ON fax_ocr_token(fax_page_id, bbox_y0, bbox_x0);

-- Full-text search on tokens
CREATE INDEX idx_ocr_token_text_trgm ON fax_ocr_token USING gin(token_text gin_trgm_ops);

-- Numeric/date filtering
CREATE INDEX idx_ocr_token_numeric ON fax_ocr_token(fax_page_id) WHERE is_numeric = true;
CREATE INDEX idx_ocr_token_date ON fax_ocr_token(fax_page_id) WHERE is_date_like = true;

-- ============================================
-- TEMPLATE INDEXES
-- ============================================

-- Active template lookup
CREATE INDEX idx_template_active ON fax_template(payer_name, doc_type) WHERE is_active = true;

-- Version lookup
CREATE INDEX idx_template_version_active ON fax_template_version(template_id) WHERE is_active = true;

-- pHash prefilter (critical for matching performance)
CREATE INDEX idx_template_sample_phash ON fax_template_sample(phash_value);

-- ============================================
-- EXTRACTION INDEXES
-- ============================================

-- Field lookup by job
CREATE INDEX idx_extracted_field_job ON fax_extracted_field(fax_job_id);

-- Field key queries
CREATE INDEX idx_extracted_field_key ON fax_extracted_field(fax_job_id, field_key);

-- ============================================
-- REVIEW INDEXES
-- ============================================

-- Review queue by priority
CREATE INDEX idx_review_queue ON fax_review(priority DESC, created_at ASC) WHERE submitted_at IS NULL;

-- Claimed reviews (for expiration checks)
CREATE INDEX idx_review_claimed ON fax_review(claim_expires_at) WHERE claimed_at IS NOT NULL AND submitted_at IS NULL;

-- ============================================
-- AUDIT INDEXES
-- ============================================

-- Audit log queries
CREATE INDEX idx_audit_tenant ON audit_log(tenant_id, created_at DESC);
CREATE INDEX idx_audit_resource ON audit_log(resource_type, resource_id);
CREATE INDEX idx_audit_user ON audit_log(user_id, created_at DESC);
