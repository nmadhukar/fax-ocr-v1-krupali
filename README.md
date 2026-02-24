# Healthcare Fax Processing System

A production-grade, HIPAA-compliant system for automated extraction of Prior Authorization data from healthcare faxes. Combines perceptual template matching, PaddleOCR, and LayoutLM Document QA to achieve high accuracy across ten major payers — with a human review layer for low-confidence fields.

## Documentation

| Document | Contents |
|---|---|
| [SETUP_AND_RUN.md](SETUP_AND_RUN.md) | **Start here** — complete client setup and daily use guide |
| [README.md](README.md) | Project overview, quick start, configuration reference |
| [README_DOCKER.md](README_DOCKER.md) | Complete Docker setup guide for Windows, macOS, and Linux |
| [README_API.md](README_API.md) | Full API reference with request/response schemas |
| [README_ARCHITECTURE.md](README_ARCHITECTURE.md) | System architecture and pipeline stage descriptions |
| [README_TECHNICAL.md](README_TECHNICAL.md) | Developer reference: codebase, algorithms, extension guide |

---

## Overview

Healthcare faxes arrive as PDFs of varying quality. This system processes them automatically through a multi-stage pipeline and returns structured, validated field values. When machine confidence is low, the document is routed to a review queue where a clinician can correct specific fields — and those corrections feed back into model fine-tuning over time.

**Supported payers:** Anthem, CareSource, Molina, Buckeye, Humana, UnitedHealthcare, AmeriHealth, Aetna, Paramount, ProMedica

**Extracted fields per document:** member ID, prior auth number, patient name, date of birth, auth effective and expiration dates, next review date, provider name, provider NPI, provider phone/fax, service code, units requested, diagnosis code, decision

**Test results:**
- Batch 3 (7 PDFs): 41/41 fields correct, average confidence 0.803
- Batch 4 (26 PDFs): 21/21 payer detections correct, average confidence 0.824
- Unit test suite: 339 tests passing

---

## Architecture

```
Upload (PDF)
     |
     v
Fax Ingress API  (port 8001)
     |
     |  queues job
     v
Celery Worker
     |
     +-- Split PDF into pages
     +-- Preprocess (deskew, denoise, binarize)
     +-- OCR  (PaddleOCR PP-OCRv5)
     +-- Cover page detection  (pixel density + OCR keyword pass)
     +-- Payer detection  (pHash perceptual hashing at 300 DPI)
     +-- Template matching  (pHash + anchor verification)
     +-- Field extraction:
     |       Template extractor  (label-anchored, primary)
     |       OCR label extractor  (regex-driven fallback)
     |       LayoutLM Document QA  (gap-fill for low-confidence fields)
     +-- Confidence scoring and merging
     +-- HITL flagging  (fields below threshold sent to review queue)
     +-- Job finalized or marked NEEDS_REVIEW
     |
     +-- COMPLETED  ->  results available via GET /v1/faxes/{id}/results
     +-- NEEDS_REVIEW  ->  review packet available via Review API (port 8002)
```

**Infrastructure:** PostgreSQL, Redis, MinIO (all containerized)

**Models used:** impira/layoutlm-document-qa (~130 MB, Apache 2.0) — no paid APIs

---

## Quick Start (Docker — recommended)

> **New to this system?** See [SETUP_AND_RUN.md](SETUP_AND_RUN.md) for the full step-by-step client guide.

### Prerequisites

- Docker Desktop 24.0+ (running)
- 8 GB RAM, 15 GB free disk space

### 1. Build images (first time only)

```bash
docker compose build
```

### 2. Start everything

```bash
docker compose up -d
```

Migrations run automatically, templates seed automatically. First start takes 3–5 minutes.

### 3. Verify

```bash
docker ps   # all containers should show (healthy)
```

- Ingress API + Swagger: http://localhost:8001/docs
- Review API + Swagger:  http://localhost:8002/docs
- Query API + Swagger:   http://localhost:8003/docs
- MinIO console:         http://localhost:9001

### Stop

```bash
docker compose down   # data is preserved in Docker volumes
```

---

## API Reference

All endpoints require a Bearer token. Obtain one via `POST /v1/auth/login`.

### Ingress API — port 8001

#### Upload a fax

```
POST /v1/faxes/upload
Content-Type: multipart/form-data

Fields:
  file         PDF file (required)
  tenant_id    Your tenant identifier (required)
  payer_hint   Optional payer name to speed up matching
```

Response:
```json
{
  "fax_job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PENDING",
  "created_at": "2025-08-01T10:30:00Z"
}
```

#### Check job status

```
GET /v1/faxes/{fax_job_id}
```

Status values: `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`, `NEEDS_REVIEW`

#### Get extracted results

```
GET /v1/faxes/{fax_job_id}/results
```

Response:
```json
{
  "fax_job_id": "550e8400-e29b-41d4-a716-446655440000",
  "payer": "ANTHEM",
  "doc_type": "PRIOR_AUTH_FORM",
  "status": "COMPLETED",
  "overall_confidence": 0.91,
  "needs_review": false,
  "fields": {
    "patient_name":         { "value": "Jane Doe",     "confidence": 0.97, "source": "TEMPLATE_OCR", "not_present": false },
    "member_id":            { "value": "MBR001234",    "confidence": 0.98, "source": "HYBRID",        "not_present": false },
    "prior_auth_number":    { "value": "PA-00123",     "confidence": 0.90, "source": "TEMPLATE_OCR", "not_present": false },
    "auth_effective_date":  { "value": "08/07/2025",   "confidence": 1.00, "source": "HYBRID",        "not_present": false },
    "auth_expiration_date": { "value": "09/05/2025",   "confidence": 1.00, "source": "HYBRID",        "not_present": false },
    "decision":             { "value": "APPROVED",     "confidence": 0.90, "source": "TEMPLATE_OCR", "not_present": false },
    "units_requested":      { "value": null,            "confidence": 0.00, "source": "HYBRID",        "not_present": true  }
  },
  "summary": {
    "total_fields": 14,
    "fields_found": 13,
    "fields_not_present": 1,
    "fields_flagged_for_review": 0,
    "flagged_field_keys": []
  }
}
```

Field `source` values:
| Value | Meaning |
|---|---|
| `TEMPLATE_OCR` | Extracted using label-anchored template matching |
| `LAYOUTLM` | Extracted by LayoutLM Document QA model |
| `HYBRID` | Multiple sources agreed; highest confidence used |
| `HUMAN_REVIEW` | Corrected by a human reviewer (authoritative) |

#### Get raw OCR tokens

```
GET /v1/faxes/{fax_job_id}/ocr
```

#### List jobs

```
GET /v1/faxes?status_filter=NEEDS_REVIEW&limit=50&offset=0
```

Admins see all tenants. Non-admin users see only their own tenant's jobs.

---

### Review API — port 8002

#### Get review packet

Returns the full document for a job in `NEEDS_REVIEW` state, including flagged fields and per-field confidence.

```
GET /v1/faxes/{fax_job_id}/review-packet
```

Response includes:
- Page images (base64)
- All extracted fields with `is_flagged` and `flag_reason` per field
- `flagged_fields` list with threshold details

#### Claim a job for review

```
POST /v1/faxes/{fax_job_id}/review/claim
```

Prevents two reviewers from working on the same document simultaneously. Audit-logged.

#### Submit corrections

```
POST /v1/faxes/{fax_job_id}/review/submit

{
  "outcome": "APPROVED",
  "notes": "Member ID corrected from OCR misread",
  "corrected_fields": [
    { "field_key": "member_id", "corrected_value": "ABC123456789" }
  ]
}
```

Corrections are applied with `confidence = 1.0` and `source = HUMAN_REVIEW`. The original machine-extracted value is archived in the audit trail. Corrected fields are also recorded as training examples for future model fine-tuning.

---

### Template Admin — port 8002

Manage the payer form templates used for field extraction.

| Method | Path | Description |
|---|---|---|
| GET | `/v1/templates` | List all templates |
| POST | `/v1/templates` | Create a template |
| GET | `/v1/templates/{id}` | Get template detail |
| PUT | `/v1/templates/{id}` | Update template |
| DELETE | `/v1/templates/{id}` | Delete template |
| POST | `/v1/templates/{id}/versions` | Create a new version |
| POST | `/v1/templates/versions/{vid}/samples` | Upload a sample page image |
| POST | `/v1/templates/versions/{vid}/fields` | Add a field definition |
| GET | `/v1/templates/versions/{vid}/fields` | List field definitions |
| PUT | `/v1/templates/versions/{vid}/fields/{fid}` | Update a field |
| DELETE | `/v1/templates/versions/{vid}/fields/{fid}` | Remove a field |
| POST | `/v1/templates/test-match` | Test a document against templates |

---

### Analytics — port 8002

```
GET /v1/analytics/summary          Overall system metrics
GET /v1/analytics/payer-breakdown  Per-payer accuracy and volume
GET /v1/analytics/mismatches       Fields with conflicting source values
GET /v1/analytics/hourly-trend     Hourly processing volume
GET /v1/analytics/field-accuracy   Per-field accuracy rates
```

---

### Model Versions — port 8002

Tracks LayoutLM adapter versions. After each fine-tuning run a new version is registered and can be promoted to production.

```
GET    /v1/models                  List all model versions
POST   /v1/models                  Register a new version
GET    /v1/models/active           Get the currently active version
PUT    /v1/models/{id}/promote     Promote a version to production
GET    /v1/models/{id}/metrics     Get accuracy metrics for a version
POST   /v1/models/{id}/metrics     Record new metrics for a version
```

---

## Project Structure

```
fax_ocr/
|
+-- configs/
|   +-- payer_rules.yml             Field validation patterns per payer
|
+-- docker-compose.yml              PostgreSQL, Redis, MinIO
+-- docker-entrypoint.sh
+-- Dockerfile
+-- Makefile
+-- pyproject.toml
+-- requirements.txt
|
+-- infra/
|   +-- migrations/                 001 through 009 — run in order
|
+-- services/
|   +-- fax_ingress_api/            Upload and query API  (port 8001)
|   +-- fax_review_api/             Review, template admin, analytics (port 8002)
|
+-- workers/
|   +-- fax_processing_worker/      Celery pipeline worker + task scheduler
|
+-- libs/shared/
|   +-- config/                     Settings, environment variables
|   +-- db/                         SQLAlchemy models and repositories
|   +-- storage/                    MinIO adapter
|   +-- ocr/                        PaddleOCR client (subprocess-isolated)
|   +-- template/                   pHash matching, anchor scoring
|   +-- extraction/                 Template extractor, OCR label extractor,
|   |                               LayoutLM extractor, field builder,
|   |                               HITL flagging, output formatter
|   +-- vlm/                        LayoutLM client and base interfaces
|   +-- classification/             Document type and cover page detection
|   +-- monitoring/                 Pipeline metrics, mismatch alerts
|   +-- utils/                      Image preprocessing utilities
|
+-- scripts/
|   +-- seed_templates.py           Load payer templates into DB (run once)
|   +-- generate_training_data.py   Build LayoutLM training set from DB
|   +-- finetune_layoutlm.py        Fine-tune LayoutLM on labeled examples
|   +-- export_training_data.py     Export training data to disk
|   +-- do_upload.py                Helper: upload a PDF via the API
|   +-- start_services.ps1          Start all services (Windows PowerShell)
|
+-- tests/
|   +-- unit/                       339 unit tests (run with pytest)
|   +-- integration/
|
+-- pdfs/
|   +-- Templetes_pdf/              Source PDFs for seed_templates.py
|   +-- Client_response_for_templete/  Reference documents
|
+-- extras/                         Development artifacts (not for deployment)
    +-- dev_scripts/
    +-- debug_scripts/
    +-- specs/
    +-- logs/
    +-- test_output/
    +-- test_pdfs/
```

---

## Configuration

All settings are controlled via environment variables or a `.env` file in the project root.

### Core settings

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql+psycopg2://faxadmin:faxpass123@127.0.0.1:5432/fax_processor` |
| `REDIS_URL` | Redis URL | `redis://127.0.0.1:6379/0` |
| `CELERY_BROKER_URL` | Celery broker | `redis://127.0.0.1:6379/0` |
| `MINIO_ENDPOINT` | MinIO host:port | `127.0.0.1:9000` |
| `MINIO_ACCESS_KEY` | MinIO access key | `minioadmin` |
| `MINIO_SECRET_KEY` | MinIO secret key | `minioadmin123` |
| `SECRET_KEY` | JWT signing key | — (required in production) |

### Processing settings

| Variable | Description | Default |
|---|---|---|
| `ENABLE_VLM` | Enable LayoutLM gap-fill extraction | `true` |
| `CONFIDENCE_AUTO_FINALIZE` | Score above which a job auto-completes | `0.90` |
| `CONFIDENCE_NEEDS_REVIEW` | Score below which job goes to review | `0.65` |
| `OCR_ENABLE_GPU` | Use GPU for PaddleOCR | `false` |

### HITL settings

| Variable | Description | Default |
|---|---|---|
| `HITL_ENABLED` | Enable per-field review flagging | `true` |
| `HITL_DEFAULT_THRESHOLD` | Flag fields below this confidence | `0.75` |
| `HITL_CRITICAL_THRESHOLD` | Higher threshold for critical fields | `0.85` |
| `HITL_MIN_FLAGS_FOR_REVIEW` | Number of flags that triggers NEEDS_REVIEW | `1` |

Critical fields (patient_name, member_id, auth dates, decision, service_code, diagnosis_code) use the higher threshold. All other fields use the default threshold.

### Security settings (production)

| Variable | Description |
|---|---|
| `API_ALLOWED_HOSTS` | Comma-separated allowed hostnames |
| `SECRET_KEY` | Minimum 32-character random string |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | Must not be the default values |

The application raises a startup error if default credentials are used in a production environment.

---

## Human-in-the-Loop Review

When a processed document has fields below the confidence threshold, the job status is set to `NEEDS_REVIEW` and the document appears in the review queue.

**Reviewer workflow:**
1. Call `GET /v1/faxes/{id}/review-packet` to receive the document with flagged fields highlighted
2. Call `POST /v1/faxes/{id}/review/claim` to lock the document
3. Review each flagged field against the document image
4. Call `POST /v1/faxes/{id}/review/submit` with any corrections

Corrections are applied with full confidence and recorded as training examples. When enough corrections accumulate (`RETRAIN_MIN_NEW_LABELS`, default 20), the weekly retraining task fine-tunes the LayoutLM adapter, which is then registered as a new model version and can be promoted to production.

---

## LayoutLM Fine-tuning

The LayoutLM model can be fine-tuned on verified extractions to improve accuracy on your specific document population.

**Step 1 — Generate training data:**
```bash
python scripts/generate_training_data.py --source all
```
This reads verified jobs and seeded templates from the database and writes labeled examples to the `fax_label_example` table.

**Step 2 — Fine-tune:**
```bash
python scripts/finetune_layoutlm.py
```
Produces a LoRA adapter saved to disk. The adapter path is printed on completion.

**Step 3 — Register and promote:**
Use `POST /v1/models` to register the adapter, then `PUT /v1/models/{id}/promote` to make it active.

The active adapter is loaded automatically when the worker starts.

---

## Running Tests

```bash
pytest tests/unit/ -v
```

For a coverage report:
```bash
pytest tests/unit/ --cov=libs --cov=services --cov=workers --cov-report=term-missing
```

---

## Database Migrations

Migrations are plain SQL files in `infra/migrations/` and must be run in order. They are idempotent — safe to re-run.

| File | Contents |
|---|---|
| 001_initial_schema.sql | Core tables: fax_job, fax_page, fax_ocr_token, fax_template, fax_extraction |
| 002_indexes.sql | Performance indexes |
| 003_pgvector.sql | pgvector extension (not required if unused) |
| 004_week2_enums.sql | Additional enum values |
| 005_mismatch_metric.sql | Mismatch tracking table |
| 006_label_example.sql | LayoutLM training data table |
| 007_model_version.sql | Model version registry |
| 008_template_config.sql | Template configuration columns |
| 009_hitl_flagged_fields.sql | HITL flagged_fields column on fax_extraction |

---

## Service Ports

| Service | Port | Purpose |
|---|---|---|
| Fax Ingress API | 8001 | Upload, status, results |
| Fax Review API | 8002 | Review queue, templates, analytics |
| PostgreSQL | 5432 | Primary database |
| Redis | 6379 | Task queue and cache |
| MinIO API | 9000 | Object storage |
| MinIO Console | 9001 | Web UI for MinIO |

---

## Default Credentials

Change all of these before any deployment outside a local development machine.

| Service | Username | Password |
|---|---|---|
| PostgreSQL | faxadmin | faxpass123 |
| MinIO | minioadmin | minioadmin123 |

---

## Security

- JWT authentication is required on all API endpoints.
- Every access to patient data is written to the `audit_log` table (HIPAA requirement).
- Tenant isolation is enforced at the repository layer — users cannot query another tenant's jobs.
- File uploads are validated for type, size, and content before processing.
- Security headers (HSTS, X-Content-Type-Options, X-Frame-Options, CSP) are set on all responses.
- Default credentials cause a startup error in production mode.

---

## License

Proprietary. All rights reserved.
