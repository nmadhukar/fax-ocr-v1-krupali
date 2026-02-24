-- 008: Add template_config JSONB column to fax_template_version
-- Stores: rotate_pages, content_pages, cover_pages metadata
ALTER TABLE fax_template_version ADD COLUMN IF NOT EXISTS template_config JSONB DEFAULT '{}';
