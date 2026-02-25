# Healthcare Fax OCR Processing System — Architecture & System Overview

**Version:** 1.0.0
**Project:** `healthcare-fax-processor`
**Python:** 3.11+
**License:** Proprietary

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Architecture](#2-system-architecture)
3. [Infrastructure Components](#3-infrastructure-components)
4. [Service Architecture](#4-service-architecture)
5. [Processing Pipeline — 17 Stages](#5-processing-pipeline--17-stages)
6. [Data Model](#6-data-model)
7. [Extraction Engine](#7-extraction-engine)
8. [Template Matching System](#8-template-matching-system)
9. [Confidence Scoring & Merging](#9-confidence-scoring--merging)
10. [Human-in-the-Loop (HITL) Review](#10-human-in-the-loop-hitl-review)
11. [Cross-Field Validation](#11-cross-field-validation)
12. [Payer Configuration System](#12-payer-configuration-system)
13. [Security & HIPAA Compliance](#13-security--hipaa-compliance)
14. [ML Model Management](#14-ml-model-management)
15. [Query & Semantic Search](#15-query--semantic-search)
16. [Monitoring & Analytics](#16-monitoring--analytics)
17. [Operations Console UI](#17-operations-console-ui)
18. [Configuration Reference](#18-configuration-reference)

---

## 1. Executive Summary

This is a **production-grade, HIPAA-compliant, multi-tenant fax processing system** designed for healthcare prior-authorization workflows. It ingests fax documents (PDF, TIFF, images), runs them through a 17-stage ML pipeline, extracts structured data fields, and routes uncertain results for human review.

### Key Design Principles

- **Zero external API calls** — All ML models (PaddleOCR, LayoutLM, sentence-transformers) run locally; no data leaves the network
- **Multi-tenant isolation** — Every record is scoped by `tenant_id`; API enforces tenant boundaries via JWT
- **HIPAA-grade audit trail** — Every read, write, download, and correction is logged to the `audit_log` table
- **Template-driven extraction** — Primary extraction uses label-anchored ROI templates with perceptual hash matching
- **Three-source fusion** — Template OCR + regex-based OCR + LayoutLM Document QA results are merged via weighted scoring
- **Human-in-the-Loop** — Low-confidence fields are flagged; reviewers claim, correct, and feed corrections back for retraining
- **Self-improving** — Human corrections generate LayoutLM training labels; weekly LoRA fine-tuning improves accuracy over time

### Technology Stack

| Layer | Technology |
|-------|-----------|
| **Operations Console** | Vanilla JS + HTML5 + CSS3 (zero-dependency SPA) |
| **API Framework** | FastAPI 0.109 + Uvicorn 0.27 |
| **Database** | PostgreSQL 15 + pgvector |
| **Task Queue** | Celery 5.3 + Redis 7 |
| **Object Storage** | MinIO (S3-compatible) |
| **OCR Engine** | PaddleOCR PP-OCRv5 (English) |
| **VLM** | LayoutLM Document QA (impira/layoutlm-document-qa) |
| **Embeddings** | sentence-transformers/all-MiniLM-L6-v2 |
| **Image Processing** | OpenCV 4.6, Pillow, scikit-image |
| **Fine-Tuning** | PEFT/LoRA adapters for LayoutLM |

---

## 2. System Architecture

```
                    ┌─────────────────────────────────────────────────────────────────┐
                    │                     EXTERNAL CLIENTS                            │
                    │              (EMR Systems, Fax Gateways, Review UI)             │
                    └───────────┬─────────────────┬─────────────────┬─────────────────┘
                                │                 │                 │
                    ┌───────────▼──────┐ ┌────────▼────────┐ ┌─────▼──────────┐
                    │  Fax Ingress API │ │  Fax Review API │ │  Fax Query API │
                    │     :8001        │ │     :8002        │ │     :8003      │
                    │                  │ │                  │ │                │
                    │  POST /upload    │ │  Templates CRUD  │ │  POST /query   │
                    │  GET  /status    │ │  Review workflow │ │  Tier 1: SQL   │
                    │  GET  /results   │ │  Analytics       │ │  Tier 2: Vector│
                    │  GET  /ocr       │ │  Model versions  │ │                │
                    │  DELETE /{id}    │ │                  │ │                │
                    └───────┬──────────┘ └────────┬─────────┘ └────────────────┘
                            │                     │
                            │    ┌────────────────┤
                            │    │                │
                    ┌───────▼────▼──┐     ┌───────▼────────────────────────────┐
                    │    Redis 7    │     │          PostgreSQL 15             │
                    │   (Broker +   │     │        + pgvector extension        │
                    │    Cache)     │     │                                    │
                    │   :6379       │     │   fax_job, fax_page, fax_ocr_token │
                    └───────┬───────┘     │   fax_template*, fax_extraction    │
                            │             │   fax_review, fax_feedback         │
                    ┌───────▼───────┐     │   fax_embedding, audit_log        │
                    │ Celery Worker │     │   model_version, fax_label_example │
                    │               │     └────────────────────────────────────┘
                    │ 17-Stage      │
                    │ Pipeline      │             ┌────────────────────┐
                    │               │◄────────────│      MinIO         │
                    │ PaddleOCR     │             │   (S3-compatible)  │
                    │ LayoutLM      │             │   :9000 / :9001    │
                    │ ORB+pHash     │             │                    │
                    │ FieldBuilder  │             │   Buckets:         │
                    └───────────────┘             │   - fax-documents  │
                                                  │   - templates      │
                    ┌───────────────┐             └────────────────────┘
                    │ Celery Beat   │
                    │ (Scheduler)   │
                    │               │
                    │ Hourly:       │
                    │  mismatch-    │
                    │  monitor      │
                    │               │
                    │ Weekly:       │
                    │  layoutlm-    │
                    │  retrain      │
                    └───────────────┘
```

### Data Flow

1. **Upload** → Client sends PDF/TIFF to Ingress API → stored in MinIO → job created in PostgreSQL → task queued in Redis
2. **Process** → Celery worker dequeues task → runs 17-stage pipeline → extraction results stored in PostgreSQL
3. **Review** → If `needs_review=true`, reviewer UI fetches review packet from Review API → reviewer claims, corrects, submits
4. **Query** → Downstream systems query results via Query API (SQL lookup or vector search)
5. **Retrain** → Human corrections generate training labels → weekly Celery beat task fine-tunes LayoutLM adapter

---

## 3. Infrastructure Components

### PostgreSQL 15 + pgvector

- **Image:** `pgvector/pgvector:pg15`
- **Port:** 5432
- **Extensions:** `uuid-ossp`, `vector`, `pg_trgm`
- **Connection Pooling:** SQLAlchemy 2.0 with configurable pool_size (default 10) and max_overflow (default 20)

**Key tables:** `fax_job`, `fax_page`, `fax_ocr_token`, `fax_template`, `fax_template_version`, `fax_template_field`, `fax_template_sample`, `fax_extraction`, `fax_extracted_field`, `fax_review`, `fax_feedback`, `fax_embedding`, `fax_label_example`, `model_version`, `audit_log`

### Redis 7

- **Image:** `redis:7-alpine`
- **Port:** 6379
- **Usage:** Celery broker (DB 0), Celery result backend (DB 1)
- **Persistence:** append-only file (AOF)

### MinIO

- **Image:** `minio/minio:RELEASE.2024-01-29T03-56-32Z`
- **Ports:** 9000 (API), 9001 (Console)
- **Buckets:** `fax-documents` (uploaded PDFs, page images), `templates` (sample images)
- **Access:** Private (no anonymous access); presigned URLs for browser access

---

## 4. Service Architecture

The system is split into **3 stateless API services** and **1 stateful worker**:

### Fax Ingress API (Port 8001)

**Responsibility:** File upload, job management, result retrieval

- Upload fax files (PDF, TIFF, PNG, JPEG) with SHA-256 deduplication
- File magic-byte validation to prevent extension spoofing
- Rate limiting per tenant
- Returns clean client-facing extraction results via `output_formatter`

### Fax Review API (Port 8002)

**Responsibility:** Template management, human review workflow, analytics, model version registry, Operations Console UI

- **Operations Console** — Browser-based SPA at `/ui` for all system workflows (see [Section 17](#17-operations-console-ui))
- Full CRUD for templates with versioning (template → version → fields → samples)
- Review workflow: claim → correct → submit with optimistic locking
- Analytics dashboards: quality metrics, per-payer stats, feedback summaries
- LayoutLM model version registry with promote/rollback

### Fax Query API (Port 8003)

**Responsibility:** Read-only result queries

- **Tier 1:** Structured SQL lookup by field key or value (ILIKE)
- **Tier 2:** Semantic vector search using pgvector cosine similarity
- **Tier 3:** Summariser (stub — not yet implemented)

### Celery Worker

**Responsibility:** Stateful ML pipeline execution

- Keeps ML models (PaddleOCR, LayoutLM, sentence-transformers) loaded in memory as singletons
- LayoutLM uses double-checked locking for thread-safe initialization
- Memory limit: 4GB (2GB reserved)
- Auto-retry with 3 max retries, 60s delay for transient errors

### Celery Beat Scheduler

**Responsibility:** Periodic maintenance tasks

- **Hourly:** `aggregate_mismatch_metrics` — detects field-level extraction anomalies
- **Weekly:** `check_and_retrain_layoutlm` — fine-tunes LayoutLM adapter on new labeled examples

---

## 5. Processing Pipeline — 17 Stages

The pipeline is implemented in `workers/fax_processing_worker/tasks/stages/`:

### Stage 1–3: Ingestion (`stages/ingestion.py`)

| Step | Operation | Details |
|------|-----------|---------|
| 1 | **Load & Split** | Download from MinIO → split PDF/TIFF into page images (300 DPI, max 100 pages) |
| 2 | **Preprocess** | Deskew, denoise, binarize, contrast enhancement via `ImagePreprocessor` |
| 3 | **OCR** | PaddleOCR PP-OCRv5 — detection + recognition + angle classification |
| 3b | **Cover Page Re-detection** | Keyword matching ("cover sheet", "facsimile", etc.) + pixel density heuristics |
| 3c | **Composite Document Detection** | Identifies multi-document faxes (e.g., auth form + clinical notes) |
| 3d | **Embeddings** | sentence-transformers encodes page text → stored in `fax_embedding` for vector search |

### Stage 4–6: Classification (`stages/classification.py`)

| Step | Operation | Details |
|------|-----------|---------|
| 4 | **Payer Detection** | Keyword-based matching against known payer signatures |
| 5 | **Template Matching** | pHash (300 DPI) + ORB feature matching + FLANN + anchor scoring |
| 5b | **Page Rotation** | Auto-correct page orientation if template match suggests rotation |
| 6 | **Document Classification** | Keyword/regex classification into DocTypeEnum (PRIOR_AUTH_FORM, etc.) |

### Stage 7–10: Extraction (`stages/extraction.py`)

| Step | Operation | Details |
|------|-----------|---------|
| 7 | **Template Extraction** | PRIMARY — Label-anchored ROI extraction using matched template fields |
| 8 | **OCR Label Extraction** | FALLBACK — Regex-driven pattern matching on OCR text (LABEL_ALIASES) |
| 9 | **LayoutLM Extraction** | GAP-FILL — LayoutLM Document QA for fields with low/missing confidence |
| 9b | **Decision from Classifier** | Adds `decision` field (APPROVED/DENIED) from document type classification |
| 9c | **VLM Pre-screening** | Validates VLM outputs against known corruption patterns (fax headers, DOB=auth date) |

### Stage 11–14: Post-Processing (`stages/post_processing.py`)

| Step | Operation | Details |
|------|-----------|---------|
| 10 | **FieldBuilder Merge** | Merges 3 extraction sources using `FieldBuilder` with weighted scoring |
| 10b | **Smart Corrections** | Applies heuristic fixes (e.g., units_requested cleanup, payer-specific normalization) |
| 10c | **OCR Scanners** | Additional regex scanners for missed fields (auth numbers, dates) |
| 11 | **Canonicalization** | Normalize dates (MM/DD/YYYY→YYYY-MM-DD), uppercase IDs, remove spaces/dashes |
| 12 | **Field Validation** | Type-specific validation: NPI Luhn check, phone 10-digit, date range, regex patterns |

### Stage 15: Validation (`stages/validation.py`)

| Step | Operation | Details |
|------|-----------|---------|
| 13 | **Cross-Field Validation** | Date ordering, decision consistency, VLM contamination detection |
| 14 | **Confidence Scoring** | Weighted merge across sources → compute overall_conf |
| 15 | **Review Determination** | If overall_conf < auto_finalize threshold → `NEEDS_REVIEW` |

### Stage 16–17: Finalization (`stages/finalization.py`)

| Step | Operation | Details |
|------|-----------|---------|
| 16 | **Store Extraction** | Write final extraction_json to `fax_extraction` table |
| 16b | **HITL Flagging** | `compute_field_flags()` flags low-confidence fields → stored in `flagged_fields` |
| 17a | **Store Job Metadata** | Update `fax_job` with final status, confidence, payer, doc_type |
| 17b | **Create Review or Finalize** | If `needs_review` → create `fax_review` record; else → `COMPLETED` |
| 17c | **Finalize Metrics** | Log pipeline timing, field counts, and method distribution |

---

## 6. Data Model

### Entity Relationship Diagram

```
fax_job (1) ─────┬───── (*) fax_page ────── (*) fax_ocr_token
                 │
                 ├───── (1) fax_extraction
                 │
                 ├───── (*) fax_extracted_field
                 │
                 ├───── (1) fax_review ────── (*) fax_feedback
                 │
                 ├───── (*) fax_label_example
                 │
                 └───── (*) fax_embedding

fax_template (1) ─── (*) fax_template_version (1) ─┬── (*) fax_template_field
                                                    └── (*) fax_template_sample

model_version (standalone)
audit_log (standalone)
```

### Key Columns

**fax_job:**
- `fax_job_id` (UUID PK), `tenant_id`, `original_filename`, `file_storage_key`, `file_sha256`
- `status` (PENDING → PROCESSING → COMPLETED/NEEDS_REVIEW/FAILED)
- `payer_hint` (PayerNameEnum), `doc_type` (DocTypeEnum), `overall_conf` (0.0–1.0)
- `needs_review` (bool), `matched_template_version_id` (FK)

**fax_extraction.extraction_json** (JSONB — per-field structure):
```json
{
  "member_id": {
    "value": "MBR001234",
    "confidence": 0.95,
    "method": "TEMPLATE_OCR",
    "evidence_bbox": {"page": 1, "x0": 0.1, "y0": 0.2, "x1": 0.4, "y1": 0.25},
    "evidence_text": "Member ID: MBR001234",
    "candidates": [
      {"value": "MBR001234", "method": "TEMPLATE_OCR", "confidence": 0.95},
      {"value": "MBR001234", "method": "LAYOUTLM", "confidence": 0.82}
    ],
    "validation_passed": true,
    "validation_errors": [],
    "not_present": false
  }
}
```

---

## 7. Extraction Engine

The extraction engine uses a three-source fusion approach:

### Source 1: Template Extractor (PRIMARY)

**Module:** `libs/shared/extraction/template_extractor.py`

- Uses matched template's field definitions (ROI coordinates)
- Crops each field's bounding box from the page image
- Runs PaddleOCR on the cropped region
- Highest trust — gets confidence bonus from `FieldBuilder`

### Source 2: OCR Label Extractor (FALLBACK)

**Module:** `libs/shared/extraction/ocr_label_extractor.py`

- Scans full-page OCR text using regex patterns (`LABEL_ALIASES`)
- Finds label text (e.g., "Member ID:", "Auth #:") and extracts adjacent values
- Works on any document — no template required

### Source 3: LayoutLM Document QA (GAP-FILL)

**Module:** `libs/shared/extraction/layoutlm_extractor.py`

- Uses `impira/layoutlm-document-qa` transformer model
- Only invoked for fields with confidence < threshold or missing values
- Supports LoRA adapter for fine-tuned models
- Score multiplier: 0.70 (base model), 1.20 (fine-tuned)
- Singleton pattern — model loaded once per worker process

### FieldBuilder Merge

**Module:** `libs/shared/extraction/field_builder.py`

The `FieldBuilder` merges candidates from all three sources:

1. **Template bonus:** Template OCR candidates get a +0.15 confidence boost
2. **Agreement bonus:** When 2+ sources agree on the same value → +0.20 boost (only for non-empty values)
3. **VLM multiplier:** LayoutLM candidates scaled by `settings.vlm.layoutlm_score_multiplier`
4. **Winner selection:** Highest-scoring candidate wins
5. **not_present detection:** If best confidence < `field_min` threshold → field marked as not_present

---

## 8. Template Matching System

**Modules:** `libs/shared/template/matcher.py`, `phash.py`, `orb_matcher.py`

### Matching Algorithm

1. **Perceptual Hash Pre-filter** — Compute pHash of input at 300 DPI, compare against all template samples
   - Hamming distance threshold: configurable per version (default 5)
   - Eliminates ~95% of candidates cheaply

2. **ORB Feature Matching** — For candidates passing pHash filter:
   - Extract 500 ORB keypoints + descriptors
   - FLANN-based matching with Lowe's ratio test (0.7)
   - Minimum matches threshold: configurable per version (default 20)

3. **Combined Scoring** — Weighted combination of pHash distance and ORB inlier ratio
   - Score >= `match_min_score` (default 0.75) → match confirmed

### Template Hierarchy

```
Template (payer + doc_type + name)
  └── Version (thresholds, is_active)
        ├── Sample (image + pHash + ORB features)
        └── Field (field_key + ROI coordinates + validation)
```

---

## 9. Confidence Scoring & Merging

**Module:** `libs/shared/scoring/confidence_scorer.py`

The `ConfidenceScorer` computes the final overall confidence:

1. Per-field confidence = weighted average of:
   - Best candidate confidence (0.6 weight)
   - Agreement factor (0.2 weight)
   - Validation pass bonus (0.2 weight)

2. Overall confidence = weighted mean of all field confidences:
   - Critical fields (member_id, auth dates, decision) get 2x weight
   - Non-critical fields get 1x weight

3. **Auto-finalize threshold:** If overall_conf >= `CONFIDENCE_AUTO_FINALIZE` (default 0.90) → `COMPLETED`
4. **Review routing:** If overall_conf < threshold → `NEEDS_REVIEW`

---

## 10. Human-in-the-Loop (HITL) Review

**Module:** `libs/shared/extraction/hitl.py`

### Flagging Logic

- Each field has a confidence threshold (critical: 0.85, non-critical: 0.75)
- Fields below threshold are flagged with reason: `LOW_CONFIDENCE` or `MISSING_VALUE`
- Flags are stored in `fax_extraction.flagged_fields` (JSONB array)
- If `flagged_count >= HITL_MIN_FLAGS_FOR_REVIEW` → job status set to `NEEDS_REVIEW`

### Review Workflow

1. **List unclaimed reviews** → `GET /v1/faxes/reviews/unclaimed`
2. **Claim review** → `POST /v1/faxes/{id}/review/claim` (optimistic locking with `claim_expires_at`)
3. **Get review packet** → `GET /v1/faxes/{id}/review-packet` (pages with presigned URLs, flagged fields)
4. **Submit corrections** → `POST /v1/faxes/{id}/review/submit` (validated against allowlist)
5. **Corrections applied** → `extraction_json` updated with `method=HUMAN_REVIEW`, `confidence=1.0`
6. **Training labels created** → `fax_label_example` records written for LayoutLM fine-tuning

---

## 11. Cross-Field Validation

**Module:** `libs/shared/extraction/cross_field_validator.py`

| Check | Type | Description |
|-------|------|-------------|
| Date ordering | Error | `auth_expiration_date` must be after `auth_effective_date` |
| Decision consistency | Warning | If APPROVED, `units_requested` should exist |
| DOB in past | Error | `patient_dob` must be before today |
| Diagnosis codes | Warning | Must match ICD-10 format: `^[A-Z]\d{2}(\.[A-Za-z0-9]{1,4})?$` |
| Procedure codes | Warning | Must match CPT (5 digits) or HCPCS (letter+4 digits) |
| Units ≠ Member ID | Error | VLM contamination: `units_requested` matches `member_id` |
| Next Review ≠ DOB | Error | VLM returned DOB for `next_review_date` |
| Auth Dates ≠ DOB | Error | VLM returned DOB for auth effective/expiration dates |
| VLM Date Confusion | Warning | Same date appears in 3+ date fields |
| Auth Not Prose | Warning | `prior_auth_number` has >4 words (prose instead of ID) |

---

## 12. Payer Configuration System

**Config file:** `configs/payer_rules.yml`

### Supported Payers (10)

| Enum Value | Display Name | Critical Fields |
|------------|-------------|-----------------|
| ANTHEM | Anthem Blue Cross Blue Shield | member_id, prior_auth_number, auth dates, decision |
| UNITED_HEALTH | UnitedHealthcare | member_id, prior_auth_number, auth_effective_date, service_code, decision |
| HUMANA | Humana | member_id, prior_auth_number, auth_effective_date, decision |
| MOLINA | Molina Healthcare | member_id, prior_auth_number, decision |
| CARESOURCE | CareSource | member_id, prior_auth_number, decision |
| BUCKEYE | Buckeye Health Plan | member_id, prior_auth_number, decision |
| AMERIHEALTH | AmeriHealth Caritas | member_id, prior_auth_number, decision |
| AETNA | Aetna | member_id, prior_auth_number, decision |
| PARAMOUNT | Paramount Advantage | member_id, prior_auth_number, auth_effective_date, decision |
| PROMEDICA | ProMedica | member_id, prior_auth_number, decision |

### Per-Payer Configuration

Each payer defines:
- **field_validations** — regex patterns, length constraints
- **confidence_thresholds** — per-field minimum confidence
- **canonicalization** — normalization rules (uppercase, remove spaces/dashes, date format)

---

## 13. Security & HIPAA Compliance

### Authentication

- **JWT tokens** — Generated via `python-jose` with HS256
- **AuthUser** — Contains `user_id`, `tenant_id`, `is_admin`
- **Development bypass** — In `ENVIRONMENT=development`, tenant checks are relaxed

### Tenant Isolation

- Every API endpoint enforces `job.tenant_id == user.tenant_id`
- Admin users can access all tenants
- List endpoints filter by authenticated tenant

### Audit Logging

- **Module:** `libs/shared/security/audit.py`
- **Table:** `audit_log` — immutable append-only
- **Events logged:** CREATE, READ, UPDATE, DELETE, DOWNLOAD, REVIEW_CLAIM, REVIEW_SUBMIT
- **Fields:** tenant_id, user_id, action, resource_type, resource_id, details (JSONB), ip_address, user_agent

### File Security

- **Magic-byte validation** — Prevents content-type spoofing
- **Filename sanitization** — Prevents path traversal, null bytes, double extensions
- **Upload size limits** — Configurable max (default 50MB)
- **Rate limiting** — Per-tenant upload rate limits

### Production Safety

- `SECRET_KEY` must be ≥32 characters and not a default value
- `MINIO_SECRET_KEY` must not be "minioadmin123"
- `DATABASE_URL` must not contain "faxpass123"
- Non-root container user (`appuser`)
- Security headers: HSTS, CSP, X-Frame-Options, X-Content-Type-Options

---

## 14. ML Model Management

### Model Version Registry

**Table:** `model_version`
- `model_type` — OCR, VLM, LAYOUTLM
- `version_tag` — e.g., "layoutlm-v1.0"
- `model_path` — Filesystem or HuggingFace path
- `is_active` — Only one active version per type
- `metrics_json` — Accuracy, F1, latency metrics
- `promoted_at`, `promoted_by` — Audit trail for promotions

### LayoutLM Fine-Tuning Pipeline

1. **Label Collection** — Human corrections → `fax_label_example` table
2. **Training Data Export** — `scripts/generate_training_data.py`
3. **Fine-Tuning** — `scripts/finetune_layoutlm.py` (LoRA adapter, PEFT)
4. **Registration** — `POST /v1/models` → register new version
5. **Promotion** — `POST /v1/models/{id}/promote` → activate for production
6. **Automatic** — Celery beat runs `check_and_retrain_layoutlm` weekly

---

## 15. Query & Semantic Search

### Tier 1: Structured SQL Lookup

- Searches `fax_extracted_field` by field_key exact match or value ILIKE
- Scoped to authenticated tenant
- Ordered by confidence DESC

### Tier 2: Semantic Vector Search

- Encodes query via sentence-transformers/all-MiniLM-L6-v2
- Cosine similarity search over `fax_embedding` table (pgvector)
- Results include similarity_score and source citations

### Tier 3: Summariser (Stub)

- Planned but not yet implemented
- Returns 501 Not Implemented

---

## 16. Monitoring & Analytics

### Pipeline Metrics

**Module:** `libs/shared/monitoring/pipeline_metrics.py`

- Per-job timing: total pipeline, per-stage
- Field extraction counts by method
- Template match success rates
- Confidence distribution histograms

### Mismatch Monitor

**Task:** `workers/fax_processing_worker/tasks/mismatch_monitor.py`

- Runs hourly via Celery beat
- Detects field-level extraction anomalies
- Aggregates mismatches per payer, per field

### Analytics Endpoints

- `GET /v1/analytics/quality` — Overall quality metrics
- `GET /v1/analytics/payer/{name}` — Per-payer stats
- `GET /v1/analytics/payers` — All payer comparison
- `GET /v1/analytics/feedback-summary` — Correction analysis
- `POST /v1/analytics/recalibrate` — Threshold recommendations

---

## 17. Operations Console UI

The system includes a browser-based Operations Console served by the Review API at `http://localhost:8002/ui`.

### Architecture

The console is a zero-dependency single-page application (no build step, no framework):

| File | Size | Purpose |
|------|------|---------|
| `services/fax_review_api/ui/index.html` | ~834 lines | HTML structure with WAI-ARIA accessibility attributes |
| `services/fax_review_api/ui/app.js` | ~1136 lines | Vanilla JavaScript application logic (IIFE pattern) |
| `services/fax_review_api/ui/styles.css` | ~1116 lines | CSS with custom properties, responsive grid layout |

All files are served as static assets via FastAPI's `StaticFiles` middleware mounted at `/ui` with `html=True`.

### Capabilities

The console provides five operational tabs:

| Tab | Features | API Services Used |
|-----|----------|-------------------|
| **Dashboard** | Visual analytics with KPI cards (total jobs, auto-finalize rate, avg confidence), processing volume chart, document type distribution, payer performance comparison, confidence distribution | Review API (8002) |
| **Workflow** | Fax upload, job listing/filtering, job inspection (status/results/OCR), review queue (unclaimed/pending), review packet workspace with inline corrections | Ingress API (8001), Review API (8002) |
| **Templates** | Template CRUD, version management with matching thresholds, sample image upload, field ROI definition, test-match, test-extract, suggest-ROI | Review API (8002) |
| **Intelligence** | Tiered query (structured/semantic/summarizer), analytics reports (quality/payer/feedback/recalibrate), model version management (list/register/promote/metrics/delete) | Query API (8003), Review API (8002) |
| **API Console** | Raw HTTP request builder targeting any service with custom method, path, query string, and JSON body | Any (configurable) |

### Integration with Backend

The console communicates with all three API services:

- **34 distinct API calls** verified against backend routes
- All path parameters use `encodeURIComponent()` to prevent path traversal
- JWT Bearer tokens are automatically attached when configured
- File uploads use native `FormData` (no Base64 encoding)
- All HTML output uses XSS-safe `escapeHtml()`/`escapeAttr()` functions

### Connection Profile

Users configure API base URLs (default: `localhost:8001/8002/8003`), JWT token, and reviewer ID in the sidebar. Settings persist in browser `localStorage`.

### Accessibility

The console meets WCAG 2.1 AA requirements:
- WAI-ARIA Tabs pattern with `role="tablist"`, `role="tab"`, `role="tabpanel"`
- `aria-live="polite"` regions for activity feed and toast notifications
- Lightbox image viewer with `role="dialog"` and `aria-modal="true"`
- `:focus-visible` outlines on all interactive elements
- `prefers-reduced-motion` media query support
- `@media print` stylesheet for document printing
- Dynamic inputs include descriptive `aria-label` attributes

### Design System

CSS custom properties define the color palette, typography, spacing, and border radii:

- **Accent:** `#0f766e` (teal) — primary actions, active tabs, links
- **Warning:** `#9a6003` — flagged fields, low-confidence indicators (WCAG AA contrast)
- **Danger:** `#b42318` — errors, delete actions
- **Success:** `#12804a` — completed status, submit confirmations
- **Typography:** Sora (sans-serif headings/UI) + IBM Plex Mono (code/data)
- **Layout:** Two-column CSS Grid (330px sidebar + fluid workspace) with responsive breakpoints at 1200px and 860px

---

## 18. Configuration Reference

### Settings Hierarchy

All configuration is managed via Pydantic v2 Settings, loaded from environment variables:

```python
Settings
  ├── database: DatabaseSettings    (DATABASE_URL, DB_POOL_SIZE, ...)
  ├── redis: RedisSettings          (REDIS_URL, CELERY_BROKER_URL, ...)
  ├── minio: MinioSettings          (MINIO_ENDPOINT, MINIO_ACCESS_KEY, ...)
  ├── ocr: OcrSettings              (OCR_ENABLE_GPU, OCR_DPI_TARGET, ...)
  ├── template: TemplateMatchingSettings  (TEMPLATE_PHASH_THRESHOLD, ...)
  ├── vlm: VlmSettings             (VLM_LAYOUTLM_MODEL_NAME, ...)
  ├── embedding: EmbeddingSettings  (EMBEDDER_MODEL_NAME)
  ├── confidence: ConfidenceSettings (CONFIDENCE_AUTO_FINALIZE, ...)
  ├── hitl: HitlSettings           (HITL_ENABLED, HITL_DEFAULT_THRESHOLD, ...)
  ├── features: FeatureFlags       (ENABLE_VLM, ENABLE_VECTOR_SEARCH, ...)
  ├── api: ApiSettings             (API_HOST, API_PORT, API_DEBUG, ...)
  └── security: SecuritySettings   (SECRET_KEY, JWT_ALGORITHM, ...)
```

See `.env.example` for the complete list of environment variables with descriptions.
