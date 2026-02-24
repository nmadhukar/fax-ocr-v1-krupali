-- Migration 006: Training data pipeline - fax_label_example table
-- Stores ground-truth labels from human review for Donut fine-tuning

CREATE TABLE IF NOT EXISTS fax_label_example (
    label_example_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    fax_job_id UUID NOT NULL REFERENCES fax_job(fax_job_id) ON DELETE CASCADE,
    fax_page_id UUID NOT NULL REFERENCES fax_page(fax_page_id) ON DELETE CASCADE,
    field_key VARCHAR(100) NOT NULL,
    ground_truth_value TEXT NOT NULL,
    ground_truth_bbox JSONB,
    payer_name payer_name_enum,
    doc_type doc_type_enum,
    page_storage_key VARCHAR(512) NOT NULL,
    source VARCHAR(50) DEFAULT 'human_review',
    is_verified BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    created_by VARCHAR(255)
);

-- Indexes for efficient training data queries
CREATE INDEX IF NOT EXISTS idx_label_example_payer
    ON fax_label_example(payer_name);
CREATE INDEX IF NOT EXISTS idx_label_example_field
    ON fax_label_example(field_key);
CREATE INDEX IF NOT EXISTS idx_label_example_job
    ON fax_label_example(fax_job_id);
CREATE INDEX IF NOT EXISTS idx_label_example_verified
    ON fax_label_example(is_verified)
    WHERE is_verified = true;
