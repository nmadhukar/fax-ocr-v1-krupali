-- Ensure extraction_method_enum contains all method values used by code.
-- Safe to run repeatedly because IF NOT EXISTS is used.

ALTER TYPE extraction_method_enum ADD VALUE IF NOT EXISTS 'OCR_LABEL';
ALTER TYPE extraction_method_enum ADD VALUE IF NOT EXISTS 'LAYOUTLM';
ALTER TYPE extraction_method_enum ADD VALUE IF NOT EXISTS 'HUMAN_REVIEW';
