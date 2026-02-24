-- Healthcare Fax Processing System - Week 2 Schema Update
-- Version: 2.0.0
-- Description: Add new enum values for Aetna payer, doc type
--              classification, and LLM extraction method.

-- ============================================
-- PAYER ENUM: Add Aetna
-- ============================================
ALTER TYPE payer_name_enum ADD VALUE IF NOT EXISTS 'AETNA';

-- ============================================
-- DOC TYPE ENUM: Add approval/denial/P2P/cover classification
-- ============================================
ALTER TYPE doc_type_enum ADD VALUE IF NOT EXISTS 'PRIOR_AUTH_APPROVAL';
ALTER TYPE doc_type_enum ADD VALUE IF NOT EXISTS 'PRIOR_AUTH_DENIAL';
ALTER TYPE doc_type_enum ADD VALUE IF NOT EXISTS 'PEER_TO_PEER_DENIAL';
ALTER TYPE doc_type_enum ADD VALUE IF NOT EXISTS 'FAX_COVER_SHEET';

-- ============================================
-- EXTRACTION METHOD ENUM: Add LLM method
-- ============================================
ALTER TYPE extraction_method_enum ADD VALUE IF NOT EXISTS 'LLM';
