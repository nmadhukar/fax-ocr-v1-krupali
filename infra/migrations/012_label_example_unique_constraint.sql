-- Migration 012: Add unique constraint on fax_label_example
-- Prevents duplicate training labels for the same (job, page, field) combination.

-- First, deduplicate existing rows (keep the most recent per group)
DELETE FROM fax_label_example
WHERE label_example_id NOT IN (
    SELECT DISTINCT ON (fax_job_id, fax_page_id, field_key) label_example_id
    FROM fax_label_example
    ORDER BY fax_job_id, fax_page_id, field_key, created_at DESC
);

-- Add unique constraint
ALTER TABLE fax_label_example
    ADD CONSTRAINT uq_label_example_job_page_field
    UNIQUE (fax_job_id, fax_page_id, field_key);
