# Fax OCR System — Architecture & Code Map

> **Purpose**: This document explains how the entire Fax OCR extraction application works and maps every feature to the specific files that implement it. Use this as your guide when enhancing or debugging any part of the system.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Project Structure](#project-structure)
3. [The 4 Services](#the-4-services)
4. [Processing Pipeline — 6 Stages](#processing-pipeline--6-stages)
5. [Shared Libraries — Code Map](#shared-libraries--code-map)
6. [Database Models — Code Map](#database-models--code-map)
7. [Repositories — Code Map](#repositories--code-map)
8. [Security Layer](#security-layer)
9. [Operations Console UI](#operations-console-ui)
10. [Configuration & Infrastructure](#configuration--infrastructure)
11. [How to Enhance Each Component](#how-to-enhance-each-component)

---

## System Overview

This is a **healthcare fax processing system** that automatically extracts structured data (patient names, member IDs, authorization numbers, dates, service codes) from incoming fax documents. The system uses a multi-method extraction approach combining **template-based OCR**, **OCR label matching**, and **LayoutLM document QA** to maximize accuracy with a human-in-the-loop review workflow for quality assurance.

### High-Level Flow

```
Fax Upload → Ingress API → Celery Worker (6-stage pipeline) → Review API / Auto-Finalize
                                                                      ↓
                                                              Human Review (if needed)
                                                                      ↓
                                                              Query API (search)
```

1. A fax file (PDF, TIFF, PNG, JPEG) is uploaded via the **Ingress API**
2. The file is stored in **S3/MinIO** and a `FaxJob` is created in **PostgreSQL**
3. A **Celery worker** picks up the job and runs it through a 6-stage, 17-step pipeline
4. If confidence is high enough → auto-finalize. Otherwise → route to **human review**
5. The **Query API** enables semantic search over processed documents using **pgvector**

---

## Project Structure

```
fax_ocr_v1_krupali/
├── services/                          # API services
│   ├── fax_ingress_api/               # Service 1: File upload + job creation
│   │   ├── main.py                    # FastAPI app (port 8001)
│   │   └── api/v1/routes/
│   │       ├── faxes.py               # Upload, list, get, delete endpoints
│   │       └── health.py              # Health check
│   ├── fax_review_api/                # Service 2: Review workflow + templates + UI
│   │   ├── main.py                    # FastAPI app (port 8002)
│   │   ├── api/v1/routes/
│   │   │   ├── review.py              # Claim, submit, release review endpoints
│   │   │   ├── templates.py           # Template CRUD, versions, samples, fields
│   │   │   ├── analytics.py           # Quality metrics, payer stats, feedback
│   │   │   ├── models.py              # Model version management
│   │   │   └── health.py              # Health check
│   │   └── ui/                        # Operations Console (static HTML/JS/CSS)
│   │       ├── index.html
│   │       ├── app.js
│   │       └── styles.css
│   └── fax_query_api/                 # Service 3: Semantic search
│       ├── main.py                    # FastAPI app (port 8003)
│       └── api/v1/routes/
│           ├── query.py               # Tier 1 (SQL) + Tier 2 (pgvector) search
│           └── health.py              # Health check
│
├── workers/                           # Background processing
│   └── fax_processing_worker/
│       ├── celery_app.py              # Celery configuration
│       └── tasks/
│           ├── process_fax.py         # Main task orchestrator
│           └── stages/                # 6 pipeline stages
│               ├── __init__.py        # PipelineContext dataclass
│               ├── ingestion.py       # Stage 1: Load, split, OCR, embeddings
│               ├── classification.py  # Stage 2: Payer, template, doc type
│               ├── extraction.py      # Stage 3: Template, label, LayoutLM
│               ├── post_processing.py # Stage 4: Merge, corrections, scanners
│               ├── validation.py      # Stage 5: Validate, cross-check, score
│               └── finalization.py    # Stage 6: Store, HITL flags, review/finalize
│
├── libs/shared/                       # Shared libraries (used by all services + worker)
│   ├── config/                        # Application configuration (Pydantic settings)
│   ├── db/                            # Database layer
│   │   ├── base.py                    # SQLAlchemy declarative base
│   │   ├── session.py                 # Engine, session factory, connection pooling
│   │   ├── models/                    # 12 ORM models
│   │   └── repositories/             # 11 repository classes
│   ├── security/                      # Auth, middleware, file validation
│   ├── ocr/                           # PaddleOCR client
│   ├── template/                      # pHash + ORB template matching
│   ├── extraction/                    # Field builder, validators, extractors
│   ├── scoring/                       # Confidence scoring engine
│   ├── classification/                # Payer detection, doc classification
│   ├── embeddings/                    # Sentence-transformer embeddings
│   ├── storage/                       # S3/MinIO adapter
│   ├── clients/                       # External system clients
│   ├── monitoring/                    # Pipeline metrics
│   └── utils/                         # Image preprocessing utilities
│
├── configs/                           # Environment-specific config files
├── infra/                             # Infrastructure (Nginx, scripts)
├── scripts/                           # Helper scripts
├── tests/                             # Test suite
├── docker-compose.yml                 # Full stack Docker composition
├── Dockerfile                         # Multi-stage container build
└── pyproject.toml                     # Python project config
```

---

## The 4 Services

### Service 1: Fax Ingress API (`services/fax_ingress_api/`)

**Purpose**: Entry point for fax file uploads.

| Feature | File | Function/Endpoint |
|---------|------|--------------------|
| Upload fax file | `api/v1/routes/faxes.py` | `POST /v1/faxes/upload` |
| List all jobs | `api/v1/routes/faxes.py` | `GET /v1/faxes/` |
| Get job details | `api/v1/routes/faxes.py` | `GET /v1/faxes/{id}` |
| Get extraction results | `api/v1/routes/faxes.py` | `GET /v1/faxes/{id}/results` |
| Get OCR text | `api/v1/routes/faxes.py` | `GET /v1/faxes/{id}/ocr` |
| Delete job | `api/v1/routes/faxes.py` | `DELETE /v1/faxes/{id}` |
| File magic-byte validation | `libs/shared/security/file_validation.py` | `validate_file_magic()` |
| Filename sanitization | `libs/shared/security/file_validation.py` | `sanitize_filename()` |
| Queue processing task | `libs/shared/queue/` | Celery task dispatch |
| App startup/shutdown | `main.py` | `create_app()`, `lifespan()` |

### Service 2: Fax Review API (`services/fax_review_api/`)

**Purpose**: Review workflow, template management, analytics, and the Operations Console UI.

| Feature | File | Function/Endpoint |
|---------|------|--------------------|
| Get review queue | `api/v1/routes/review.py` | `GET /v1/faxes/review/queue` |
| Claim a review | `api/v1/routes/review.py` | `POST /v1/faxes/review/{id}/claim` |
| Submit corrections | `api/v1/routes/review.py` | `POST /v1/faxes/review/{id}/submit` |
| Release claim | `api/v1/routes/review.py` | `POST /v1/faxes/review/{id}/release` |
| Release expired claims | `api/v1/routes/review.py` | `POST /v1/faxes/review/release-expired` |
| CRUD templates | `api/v1/routes/templates.py` | `GET/POST/PUT/DELETE /v1/templates/` |
| Template versions | `api/v1/routes/templates.py` | `POST /v1/templates/{id}/versions` |
| Template samples | `api/v1/routes/templates.py` | `POST /v1/templates/{id}/versions/{vid}/samples` |
| Template fields | `api/v1/routes/templates.py` | `POST /v1/templates/{id}/versions/{vid}/fields` |
| Quality analytics | `api/v1/routes/analytics.py` | `GET /v1/analytics/quality` |
| Payer stats | `api/v1/routes/analytics.py` | `GET /v1/analytics/payers` |
| Feedback summary | `api/v1/routes/analytics.py` | `GET /v1/analytics/feedback` |
| Model registration | `api/v1/routes/models.py` | `POST /v1/models/` |
| Model promotion | `api/v1/routes/models.py` | `POST /v1/models/{id}/promote` |
| Operations Console UI | `ui/index.html`, `ui/app.js`, `ui/styles.css` | Browser UI |

### Service 3: Fax Query API (`services/fax_query_api/`)

**Purpose**: Structured and semantic search over processed fax data.

| Feature | File | Function/Endpoint |
|---------|------|--------------------|
| Tier 1: SQL field lookup | `api/v1/routes/query.py` | `POST /v1/query` |
| Tier 2: pgvector cosine search | `api/v1/routes/query.py` | `POST /v1/query` (with `tier=2`) |
| Embedding model warm-up | `main.py` | `lifespan()` startup |

### Service 4: Fax Processing Worker (`workers/fax_processing_worker/`)

**Purpose**: Background pipeline that processes uploaded faxes.

| Feature | File |
|---------|------|
| Celery configuration | `celery_app.py` |
| Task orchestrator | `tasks/process_fax.py` |
| Pipeline state bag | `tasks/stages/__init__.py` → `PipelineContext` |
| Stages 1–6 | `tasks/stages/ingestion.py` through `finalization.py` |

---

## Processing Pipeline — 6 Stages

The pipeline is orchestrated by `workers/fax_processing_worker/tasks/process_fax.py` and delegates to 6 stage modules. All state flows through a `PipelineContext` dataclass defined in `tasks/stages/__init__.py`.

### Stage 1: Ingestion (`stages/ingestion.py`)

| Step | What It Does | Key Functions |
|------|-------------|---------------|
| 1–2 | Download file from S3, split into pages | `load_and_split()` |
| 3–4 | Preprocess pages (deskew, enhance), run PaddleOCR, store tokens | `preprocess_and_ocr()` |
| 3b | OCR-based cover page re-detection | `cover_page_redetection()` |
| 4a | Composite document detection (multi-doc faxes) | `composite_document_detection()` |
| 4b | Generate sentence-transformer embeddings for pgvector | `generate_embeddings()` |

**Dependencies**:
- `libs/shared/ocr/paddle_client.py` → PaddleOCR PP-OCRv5
- `libs/shared/utils/image_utils.py` → `ImagePreprocessor` (deskew, blur score, text density)
- `libs/shared/storage/s3_adapter.py` → S3/MinIO upload/download
- `libs/shared/embeddings/` → sentence-transformers (all-MiniLM-L6-v2)
- `libs/shared/classification/document_splitter.py` → composite document splitting

### Stage 2: Classification (`stages/classification.py`)

| Step | What It Does | Key Functions |
|------|-------------|---------------|
| 5 | Auto-detect insurance payer from OCR text | `detect_payer()` |
| 6 | Two-stage template matching (pHash + ORB/FLANN) | `match_template()` |
| 6b | Apply page rotation from template config, re-OCR | `apply_page_rotation()` |
| 7 | Document type classification (approval/denial/pend) | `classify_document()` |

**Dependencies**:
- `libs/shared/classification/payer_detector.py` → keyword/signal-based payer detection
- `libs/shared/template/matcher.py` → `TemplateMatcher` (pHash prefilter + ORB verification)
- `libs/shared/template/phash.py` → perceptual hashing
- `libs/shared/template/orb_matcher.py` → ORB feature matching with FLANN
- `libs/shared/classification/doc_classifier.py` → keyword-based doc type classification

### Stage 3: Extraction (`stages/extraction.py`)

| Step | What It Does | Key Functions |
|------|-------------|---------------|
| 8 | Template ROI-based field extraction | `template_extraction()` |
| 8b | OCR label-value pair extraction | `ocr_label_extraction()` |
| 9 | LayoutLM document QA (gap filler with thread timeout) | `layoutlm_extraction()` |
| 9c | VLM candidate pre-screening | `vlm_prescreening()` |

**Dependencies**:
- `libs/shared/extraction/template_extractor.py` → `TemplateExtractor` (ROI crop + OCR read)
- `libs/shared/extraction/ocr_label_extractor.py` → label-value pair scanning
- `libs/shared/extraction/layoutlm_extractor.py` → LayoutLM QA model
- `libs/shared/extraction/field_builder.py` → `ExtractionCandidate` dataclass, VLM preprocessing

### Stage 4: Post-Processing (`stages/post_processing.py`)

| Step | What It Does | Key Functions |
|------|-------------|---------------|
| 10 | Multi-source merge (template + label + LayoutLM → best value) | `merge_fields()` |
| 10b | Domain-specific smart corrections | `smart_corrections()` |
| 10c–d | OCR pattern scanners for field recovery | `ocr_scanners()` |

**Key correction rules in `smart_corrections()`**:
- `units_requested` duplicates `member_id` → clear units
- `next_review_date` == `patient_dob` → DOB bleed → clear
- `auth_effective_date` == `auth_expiration_date` (both VLM, matches DOB) → clear
- `auth_expiration_date` == `patient_dob` → recover from template candidate or clear
- `patient_dob` birth year too recent → clear
- `units_requested` not integer 1–9999 → clear
- `patient_name` subset of `provider_name` → contamination → clear

**OCR scanners in `ocr_scanners()`**:
- HCPCS/CPT code scanner (`_service_code_scanner`)
- Auth date range recovery: `01/01/2025 – 03/31/2025` (`_auth_date_range_recovery`)
- Service dates scanner (`_service_dates_scanner`)
- Units/visits scanner (`_units_scanner`)
- Prior auth number validation + OCR recovery (`_prior_auth_validation`)
- Patient name from "Member Name:" labels (`_patient_name_recovery`)
- Patient name from "following member:" patterns (`_following_member_scan`)
- `next_review_date` (18+ years ago) → transfer to `patient_dob` (`_next_review_to_dob_transfer`)

**Dependencies**:
- `libs/shared/extraction/field_builder.py` → `FieldBuilder` (scoring hierarchy, agreement detection, label contamination)

### Stage 5: Validation (`stages/validation.py`)

| Step | What It Does | Key Functions |
|------|-------------|---------------|
| 11 | Per-field validation + canonicalization | `validate_fields()` |
| 13 | Cross-field consistency checks | `cross_field_checks()` |
| 14 | Weighted confidence scoring (with OCR quality) | `confidence_scoring()` |
| 15 | Determine if human review is needed | `determine_review()` |

**Dependencies**:
- `libs/shared/extraction/validators.py` → `FieldValidator` (date, phone, SSN, NPI with Luhn check)
- `libs/shared/extraction/canonicalizer.py` → `FieldCanonicalizer` (date normalization, phone formatting)
- `libs/shared/extraction/cross_field_validator.py` → `CrossFieldValidator`
- `libs/shared/scoring/confidence_scorer.py` → `ConfidenceScorer` (weighted fields, payer rules, OCR quality penalties)

### Stage 6: Finalization (`stages/finalization.py`)

| Step | What It Does | Key Functions |
|------|-------------|---------------|
| 16a | Store extraction JSON with model versions | `store_extraction()` |
| 16b | HITL per-field confidence flags | `hitl_flagging()` |
| — | Store all metadata on job record | `store_job_metadata()` |
| 16–17 | Create review task or auto-finalize | `create_review_or_finalize()` |
| — | Record pipeline timing metrics | `finalize_metrics()` |

**Dependencies**:
- `libs/shared/extraction/hitl.py` → `compute_field_flags()` (per-field review flags)
- `libs/shared/clients/prior_auth_client.py` → pushes extraction to prior auth system
- `libs/shared/clients/task_client.py` → creates review task in external task system
- `libs/shared/monitoring/pipeline_metrics.py` → `PipelineMetrics` timing and summary

---

## Shared Libraries — Code Map

### `libs/shared/db/` — Database Layer

| File | Purpose |
|------|---------|
| `base.py` | SQLAlchemy declarative base |
| `session.py` | Thread-safe engine creation, session factory, connection pool, FastAPI `get_db` dependency |

### `libs/shared/security/` — Security Suite

| File | Purpose |
|------|---------|
| `auth.py` | JWT creation/verification, `AuthUser` model, FastAPI dependencies (`require_auth`, `optional_auth`, `require_admin`), dev bypass |
| `middleware.py` | OWASP security headers, request-ID injection, global exception sanitization |
| `file_validation.py` | Magic-byte file type validation, filename sanitization (path traversal, null bytes) |

### `libs/shared/ocr/` — OCR Engine

| File | Purpose |
|------|---------|
| `base.py` | Abstract `OcrClient`, `OcrResult`, `OcrToken`, `BoundingBox` dataclasses |
| `paddle_client.py` | PaddleOCR PP-OCRv5 implementation (thread-safe lazy loading, line grouping, token parsing) |

### `libs/shared/template/` — Template Matching

| File | Purpose |
|------|---------|
| `matcher.py` | `TemplateMatcher` — two-pass matching (strict then adaptive), `MatchResult` dataclass |
| `phash.py` | `PerceptualHasher` — perceptual hash computation + Hamming distance |
| `orb_matcher.py` | `OrbMatcher` — ORB feature extraction + FLANN matching with homography verification |

### `libs/shared/extraction/` — Field Extraction

| File | Purpose |
|------|---------|
| `field_builder.py` | `FieldBuilder` — multi-source merge, scoring hierarchy, `ExtractionCandidate`, VLM preprocessing |
| `template_extractor.py` | `TemplateExtractor` — ROI-based field extraction, label-anchored extraction, template verification |
| `ocr_label_extractor.py` | Label-value pair extraction from OCR tokens |
| `layoutlm_extractor.py` | LayoutLM document QA model wrapper |
| `validators.py` | `FieldValidator` — date, phone, SSN, NPI (Luhn), member ID validation |
| `canonicalizer.py` | `FieldCanonicalizer` — value normalization (dates, phones, SSNs) |
| `cross_field_validator.py` | `CrossFieldValidator` — inter-field consistency checks |
| `hitl.py` | `compute_field_flags()` — per-field HITL review flags |

### `libs/shared/scoring/` — Confidence Scoring

| File | Purpose |
|------|---------|
| `confidence_scorer.py` | `ConfidenceScorer` — weighted field scoring, payer-specific rules, OCR quality penalties, review determination |

### `libs/shared/classification/` — Document Classification

| File | Purpose |
|------|---------|
| `payer_detector.py` | `PayerDetector` — keyword/signal-based payer identification |
| `doc_classifier.py` | `DocClassifier` — document type classification (approval, denial, pend) |
| `document_splitter.py` | `DocumentSplitter` — composite fax segment detection |

### `libs/shared/embeddings/` — Vector Embeddings

| File | Purpose |
|------|---------|
| `__init__.py` | `get_embedding_client()` — sentence-transformers (all-MiniLM-L6-v2) for pgvector search |

### `libs/shared/storage/` — Object Storage

| File | Purpose |
|------|---------|
| `s3_adapter.py` | `S3StorageAdapter` — upload, download, presigned URL generation for MinIO/S3 |

### `libs/shared/clients/` — External System Clients

| File | Purpose |
|------|---------|
| `prior_auth_client.py` | `PriorAuthClient` — pushes finalized extraction to prior auth system |
| `task_client.py` | `TaskClient` — creates review tasks in external task management |

### `libs/shared/monitoring/` — Observability

| File | Purpose |
|------|---------|
| `pipeline_metrics.py` | `PipelineMetrics` — timing, field counts, quality metrics, summary logging |

### `libs/shared/utils/` — Utilities

| File | Purpose |
|------|---------|
| `image_utils.py` | `ImagePreprocessor` — deskew, blur detection, text density, cover page detection |

---

## Database Models — Code Map

All models are in `libs/shared/db/models/`:

| Model | File | Table | Key Fields |
|-------|------|-------|------------|
| `FaxJob` | `fax_job.py` | `fax_job` | `fax_job_id`, `tenant_id`, `status`, `payer_hint`, `doc_type`, `overall_conf`, `needs_review`, `job_metadata` (aliased from DB column `metadata`) |
| `FaxPage` | `fax_page.py` | `fax_page` | `fax_page_id`, `page_number`, `width_px`, `height_px`, `blur_score`, `is_cover_page` |
| `FaxOcrToken` | `fax_ocr_token.py` | `fax_ocr_token` | `token_text`, `bbox_x0/y0/x1/y1`, `confidence`, `line_number`, `word_number` |
| `FaxExtraction` | `fax_extraction.py` | `fax_extraction` | `extraction_json`, `model_versions`, `flagged_fields`, quality metrics |
| `FaxExtractedField` | `fax_extraction.py` | `fax_extracted_field` | `field_key`, `field_value`, `method`, `field_conf`, `candidates` (JSONB), `validation_passed` |
| `FaxReview` | `fax_review.py` | `fax_review` | `review_packet`, `claimed_by`, `claim_status`, `submitted_corrections`, `review_reasons` |
| `FaxFeedback` | `fax_review.py` | `fax_feedback` | `field_key`, `original_value`, `corrected_value`, `feedback_type` |
| `FaxTemplate` | `fax_template.py` | `fax_template` | `payer_name`, `doc_type`, `template_name`, `is_active` |
| `FaxTemplateVersion` | `fax_template.py` | `fax_template_version` | `version_label`, `match_min_score`, `match_phash_threshold`, `template_config` |
| `FaxTemplateSample` | `fax_template.py` | `fax_template_sample` | `phash_value`, `orb_descriptors`, `orb_keypoints`, `width_px`, `height_px` |
| `FaxTemplateField` | `fax_template.py` | `fax_template_field` | `field_key`, `roi_x0/y0/x1/y1`, `target_page`, `validation_regex`, `expected_type` |
| `ModelVersion` | `model_version.py` | `model_version` | `model_type`, `version_tag`, `is_active`, `metrics_json` |

### Entity Relationships

```
FaxJob (1) ──── (N) FaxPage ──── (N) FaxOcrToken
  │
  ├──── (N) FaxExtractedField
  ├──── (1) FaxExtraction
  └──── (1) FaxReview ──── (N) FaxFeedback

FaxTemplate (1) ──── (N) FaxTemplateVersion
                              ├──── (N) FaxTemplateSample
                              └──── (N) FaxTemplateField
```

All foreign keys use `ondelete="CASCADE"` → deleting a parent cascades to all children.

---

## Repositories — Code Map

All repositories are in `libs/shared/db/repositories/`:

| Repository | File | Model | Notable Methods |
|-----------|------|-------|-----------------|
| `BaseRepository[T]` | `base.py` | Generic | `get_by_id()`, `get_all()`, `create()`, `update()`, `delete()`, `exists()` |
| `FaxJobRepository` | `fax_job_repo.py` | `FaxJob` | `get_with_relations()`, `fetch_pending_atomic()`, `update_status()`, `get_by_sha256()` |
| `FaxPageRepository` | `fax_page_repo.py` | `FaxPage` | `get_page_with_tokens()`, `get_non_cover_pages()`, `mark_as_cover_page()` |
| `OcrTokenRepository` | `ocr_token_repo.py` | `FaxOcrToken` | `bulk_insert()`, `get_tokens_in_roi()`, `get_tokens_inside_roi()`, `search_text()` |
| `ExtractedFieldRepository` | `extraction_repo.py` | `FaxExtractedField` | `upsert_field()`, `update_validation()` |
| `ExtractionRepository` | `extraction_repo.py` | `FaxExtraction` | `upsert()`, `update_flagged_fields()` |
| `ReviewRepository` | `review_repo.py` | `FaxReview` | `claim()`, `submit()`, `release()`, `get_queue()` |
| `TemplateRepository` | `template_repo.py` | `FaxTemplate` | `get_by_payer_doctype()`, `activate_version()` |
| `TemplateVersionRepository` | `template_repo.py` | `FaxTemplateVersion` | `get_active_versions()`, `get_samples_by_phash_range()` |
| `AnalyticsRepository` | `analytics_repo.py` | (aggregation) | `quality_overview()`, `payer_stats()`, `common_corrections()`, `feedback_summary()` |
| `EmbeddingRepository` | `embedding_repo.py` | (raw SQL) | `bulk_insert_embeddings()`, `cosine_search()` (pgvector) |
| `ModelVersionRepository` | `model_version_repo.py` | `ModelVersion` | `promote()`, `register_version()`, `update_metrics()` |
| `LabelExampleRepository` | `label_example_repo.py` | — | OCR label examples for training |

---

## Security Layer

### Authentication (`libs/shared/security/auth.py`)
- **JWT-based**: All API endpoints require a Bearer token
- **Dev bypass**: When `ENVIRONMENT != "production"`, returns a synthetic admin user for local testing
- **FastAPI dependencies**: `require_auth()`, `optional_auth()`, `require_admin()`

### Middleware (`libs/shared/security/middleware.py`)
- **SecurityHeadersMiddleware**: X-Content-Type-Options, X-Frame-Options, CSP, HSTS (prod), Cache-Control `no-store` (PHI)
- **RequestIDMiddleware**: Injects `X-Request-ID` for distributed tracing
- **Global exception handlers**: Sanitizes error responses in production (no stack traces leak)

### File Validation (`libs/shared/security/file_validation.py`)
- **Magic-byte checking**: Validates PDF, TIFF, PNG, JPEG file signatures
- **Filename sanitization**: Strips path traversal (`../`), null bytes, dangerous characters

---

## Operations Console UI

The UI is a single-page application served from `services/fax_review_api/ui/`:

| File | Purpose |
|------|---------|
| `index.html` | Page structure — tabs, forms, tables, modals |
| `app.js` | All logic — API calls, rendering, form handling, charts |
| `styles.css` | Styling — dark mode, responsive layout, animations |

### UI Tabs and Features

| Tab | Features | API Endpoints Used |
|-----|----------|-------------------|
| **Dashboard** | Chart.js analytics (quality metrics, payer breakdown, processing times) | `GET /v1/analytics/quality`, `GET /v1/analytics/payers`, `GET /v1/analytics/feedback` |
| **Workflow** | Upload fax, list jobs, inspect details, review queue, submit corrections | `POST /v1/faxes/upload`, `GET /v1/faxes/`, `POST /v1/faxes/review/{id}/claim` |
| **Templates** | CRUD templates, manage versions/samples/fields | `GET/POST/PUT/DELETE /v1/templates/` |
| **Intelligence** | Query faxes (Tier 1/2), view analytics, manage models | `POST /v1/query`, `GET /v1/analytics/*`, `GET/POST /v1/models/` |

### Key JS Functions in `app.js`

| Function | Purpose |
|----------|---------|
| `apiRequest(url, options)` | Central fetch wrapper (handles auth, JSON/FormData, errors) |
| `loadDashboard()` | Fetches and renders dashboard analytics |
| `uploadFax()` | File upload form submission |
| `listJobs()` | Fetches and renders paginated job table |
| `inspectJob(id)` | Shows job details, extraction results, page images |
| `loadReviewQueue()` | Fetches available review items |
| `submitReview(id)` | Submits human corrections |
| `renderQueryResultsTable()` | Renders semantic search results |

---

## Configuration & Infrastructure

| File | Purpose |
|------|---------|
| `libs/shared/config/` | Pydantic settings classes (API, DB, OCR, VLM, HITL, scoring thresholds) |
| `.env` / `.env.example` | Environment variables |
| `docker-compose.yml` | Full stack: PostgreSQL, MinIO, Redis, 3 APIs, worker |
| `Dockerfile` | Multi-stage build for production |
| `docker-entrypoint.sh` | Container startup script |
| `Makefile` | Dev commands (`make dev`, `make test`, `make migrate`) |

---

## How to Enhance Each Component

### Adding a New Payer
1. Add enum value in `libs/shared/db/models/enums.py` → `PayerNameEnum`
2. Add detection keywords in `libs/shared/classification/payer_detector.py`
3. Add payer-specific scoring rules in `libs/shared/scoring/confidence_scorer.py`
4. Add payer-specific validation rules in `libs/shared/extraction/validators.py`
5. Create templates via the UI or API

### Adding a New Extracted Field
1. Add field questions in `libs/shared/extraction/layoutlm_extractor.py` → `FIELD_QUESTIONS_KEYS`
2. Add OCR label patterns in `libs/shared/extraction/ocr_label_extractor.py`
3. Add validation logic in `libs/shared/extraction/validators.py`
4. Add field weight in `libs/shared/scoring/confidence_scorer.py` → `FIELD_WEIGHTS`
5. Add template ROI fields via the UI for payer templates

### Adding a New OCR Scanner
1. Add a new `_your_scanner()` function in `workers/fax_processing_worker/tasks/stages/post_processing.py`
2. Call it from `ocr_scanners()`
3. Follow the pattern: check if field already exists → scan OCR text → write result with confidence + method

### Adding a New Pipeline Stage
1. Create a new file in `workers/fax_processing_worker/tasks/stages/`
2. Add any needed state to `PipelineContext` in `stages/__init__.py`
3. Register the stage in `workers/fax_processing_worker/tasks/process_fax.py`

### Adding a New API Endpoint
1. Add the route handler in the appropriate `api/v1/routes/` file
2. Wire the router in the service's `main.py` if it's a new router
3. Update `services/fax_review_api/ui/app.js` if the endpoint needs a UI

### Adding a New Model
1. Create the SQLAlchemy model in `libs/shared/db/models/`
2. Create a repository in `libs/shared/db/repositories/`
3. Register the model in `libs/shared/db/models/__init__.py`
4. Create an Alembic migration

### Modifying the Confidence Scoring
1. Edit `libs/shared/scoring/confidence_scorer.py`
2. Adjust `FIELD_WEIGHTS` for field criticality
3. Adjust payer-specific rules in `_apply_payer_rules()`
4. Adjust thresholds in `determine_needs_review()`
