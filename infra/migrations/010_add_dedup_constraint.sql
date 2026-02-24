-- Migration: 010_add_dedup_constraint.sql
-- Add unique constraint on (tenant_id, file_sha256) to prevent duplicate uploads
-- at the database level, closing the TOCTOU race condition in the upload endpoint.

-- First, clean up any existing duplicates (keep the earliest row per group)
DELETE FROM fax_job
WHERE fax_job_id NOT IN (
    SELECT DISTINCT ON (tenant_id, file_sha256) fax_job_id
    FROM fax_job
    WHERE file_sha256 IS NOT NULL
    ORDER BY tenant_id, file_sha256, created_at ASC
)
AND file_sha256 IS NOT NULL
AND EXISTS (
    SELECT 1 FROM fax_job j2
    WHERE j2.tenant_id = fax_job.tenant_id
      AND j2.file_sha256 = fax_job.file_sha256
      AND j2.fax_job_id != fax_job.fax_job_id
);

-- Add the unique constraint
ALTER TABLE fax_job
    ADD CONSTRAINT uq_fax_job_tenant_sha256
    UNIQUE (tenant_id, file_sha256);
