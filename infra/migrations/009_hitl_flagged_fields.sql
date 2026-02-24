-- =============================================================================
-- Migration 009: HITL flagged_fields column on fax_extraction
-- =============================================================================
-- Adds a JSONB column to store per-field Human-in-the-Loop (HITL) review flags.
-- Each element: {"field_key": str, "reason": str, "threshold": float, "confidence": float | null}
-- Flags are computed by libs/shared/extraction/hitl.compute_field_flags() after
-- pipeline step 16 and surfaced in the review packet.
-- =============================================================================

ALTER TABLE fax_extraction
    ADD COLUMN IF NOT EXISTS flagged_fields JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN fax_extraction.flagged_fields IS
    'Per-field HITL review flags: [{"field_key", "reason", "threshold", "confidence"}]. '
    'reason is one of: LOW_CONFIDENCE, MISSING_VALUE. '
    'Populated by compute_field_flags() in libs/shared/extraction/hitl.py.';

-- GIN index for efficient filtering (e.g. WHERE flagged_fields @> ''[{"field_key":"member_id"}]'')
CREATE INDEX IF NOT EXISTS ix_fax_extraction_flagged_fields_gin
    ON fax_extraction USING GIN (flagged_fields);
