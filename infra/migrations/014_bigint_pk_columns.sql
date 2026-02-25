-- Migration 014: Upgrade high-volume primary keys to BIGINT
-- Prevents integer overflow for tables expected to exceed 2B rows.

ALTER TABLE fax_ocr_token
    ALTER COLUMN ocr_token_id TYPE BIGINT;

ALTER TABLE audit_log
    ALTER COLUMN audit_id TYPE BIGINT;
