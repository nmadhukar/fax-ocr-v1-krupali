-- Model version tracking table
-- Tracks ML model versions (OCR, LAYOUTLM, etc.) with promotion history
-- Client requirement: "model_version table (model_type, version_tag, active, metrics_json, promoted_at)"

CREATE TABLE IF NOT EXISTS model_version (
    model_version_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    model_type VARCHAR(50) NOT NULL,         -- 'OCR', 'VLM', 'LAYOUTLM'
    version_tag VARCHAR(100) NOT NULL,       -- e.g. 'layoutlm-base-v1.0', 'PP-OCRv5'
    model_path VARCHAR(512),                 -- filesystem or HuggingFace model path
    is_active BOOLEAN DEFAULT false,         -- only one active per model_type
    metrics_json JSONB DEFAULT '{}'::jsonb,  -- accuracy, latency, field-level metrics
    config_json JSONB DEFAULT '{}'::jsonb,   -- model config (max_pages, use_gpu, etc.)
    promoted_at TIMESTAMPTZ,                 -- when this version was promoted to active
    promoted_by VARCHAR(255),                -- who promoted it
    created_at TIMESTAMPTZ DEFAULT NOW(),
    notes TEXT,                              -- release notes / changelog
    UNIQUE(model_type, version_tag)
);

-- Index for fast active model lookup
CREATE INDEX IF NOT EXISTS idx_model_version_active
    ON model_version(model_type, is_active) WHERE is_active = true;

-- Seed initial model versions
INSERT INTO model_version (model_type, version_tag, model_path, is_active, promoted_at, notes)
VALUES
    ('OCR',      'PP-OCRv5',         'paddleocr',                         true, NOW(), 'PaddlePaddle OCR v5 — default OCR engine'),
    ('LAYOUTLM', 'layoutlm-base-v1.0','impira/layoutlm-document-qa',      true, NOW(), 'LayoutLM Document QA base model — extractive VLM')
ON CONFLICT (model_type, version_tag) DO NOTHING;
