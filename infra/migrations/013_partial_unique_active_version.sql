-- Migration 013: Add partial unique index on fax_template_version
-- Ensures only one active version per template at the database level.

CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_version_per_template
    ON fax_template_version (template_id)
    WHERE is_active = TRUE;
