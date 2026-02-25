# Healthcare Fax OCR System — Setup & Operations Guide

> **Version 1.0.0 — Production Ready**
>
> This guide covers everything from first-time installation to day-to-day operations,
> including uploading faxes, reading results, and the full human review workflow.

---

## Table of Contents

1. [What This System Does](#1-what-this-system-does)
2. [Prerequisites](#2-prerequisites)
3. [First-Time Installation](#3-first-time-installation)
4. [Starting & Stopping](#4-starting--stopping)
5. [Complete Workflow — Step by Step](#5-complete-workflow--step-by-step)
   - [Step A — Upload a Fax](#step-a--upload-a-fax)
   - [Step B — Monitor Processing](#step-b--monitor-processing)
   - [Step C — Get Extraction Results](#step-c--get-extraction-results)
   - [Step D — Human Review (when required)](#step-d--human-review-when-required)
6. [API Quick Reference](#6-api-quick-reference)
7. [Supported Payers & Fields](#7-supported-payers--fields)
8. [Troubleshooting](#8-troubleshooting)
9. [Production Configuration](#9-production-configuration)

---

## 1. What This System Does

The system receives healthcare prior-authorization fax PDFs, runs them through an AI pipeline, and extracts structured data fields (member ID, auth number, decision, dates, service codes, etc.) with confidence scores.

```
PDF Fax → OCR → Payer Detection → Template Match → AI Extraction → Results API
                                                                        ↓
                                              Low-confidence fields → Review Queue → Human Correction
```

**Three API services run simultaneously:**

| Port | Service | Purpose |
|------|---------|---------|
| **8001** | Ingress API | Upload faxes, check status, get results |
| **8002** | Review API | Human review queue — claim, inspect, and correct fields |
| **8003** | Query API | Search history, analytics, model version management |

---

## 2. Prerequisites

You need **only Docker Desktop** — no Python, no pip, no manual model downloads.
Everything (Python, OCR engine, AI models, database, storage) runs inside Docker.

### Install Docker Desktop

Download from: **https://www.docker.com/products/docker-desktop/**

| OS | Notes |
|----|-------|
| Windows 10/11 | Choose **WSL 2 backend** when prompted (the default) |
| macOS | Standard install |
| Ubuntu/Debian | Install Docker Engine + Docker Compose plugin |

After installing, open Docker Desktop and wait until the taskbar icon shows **"Docker Desktop is running"**.

### Minimum System Requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| RAM | 8 GB | 16 GB |
| Free disk space | **20 GB** on the Docker drive | 30 GB |
| CPU | 4 cores | 8 cores |
| OS | Windows 10 (21H2+), macOS 12+, Ubuntu 20.04+ | — |

> **Windows users:** Docker stores its data on the C: drive by default. Make sure C: has at least 20 GB free.
> To move Docker storage to another drive: Docker Desktop → Settings → Resources → Advanced → change "Disk image location".

---

## 3. First-Time Installation

This is done **once**. After this, starting the system takes only seconds.

### Step 1 — Copy project files

Place the project folder on your machine. It must contain:

```
fax_ocr/
├── docker-compose.yml      ← orchestrates all services
├── Dockerfile              ← builds the application image
├── .env.docker             ← environment configuration
├── scripts/
├── libs/
├── services/
├── workers/
└── pdfs/
```

### Step 2 — Open a terminal in the project folder

**Windows (PowerShell or Command Prompt):**
```
cd C:\path\to\fax_ocr
```

**macOS / Linux:**
```bash
cd /path/to/fax_ocr
```

### Step 3 — Build the Docker image

```bash
docker compose build
```

This single command:
- Installs all Python packages (FastAPI, PaddleOCR, PyTorch, LayoutLM, etc.)
- Downloads and bakes in all AI models (~320 MB total):
  - PaddleOCR English OCR models
  - LayoutLM Document QA model (field extraction)
  - sentence-transformers (semantic search)
- Downloads Swagger UI for the browser API console
- Compiles everything into one self-contained Docker image

**This takes 10–20 minutes on first run** (depends on internet speed).
Subsequent builds are much faster due to Docker layer caching.

When complete you will see:
```
 ✔ fax-ingress  Built
 ✔ fax-review   Built
 ✔ fax-query    Built
 ✔ fax-worker   Built
 ✔ fax-beat     Built
```

### Step 4 — Start the system

```bash
docker compose up -d
```

**First start takes 2–4 minutes** because it:
- Initialises the PostgreSQL database (runs migrations automatically)
- Seeds all 8 payer templates into the database
- No model downloads — everything is already in the image

Check that everything started correctly:
```bash
docker ps
```

All containers should show `healthy` or `Up`:
```
CONTAINER        STATUS
fax_postgres     Up (healthy)
fax_redis        Up (healthy)
fax_minio        Up (healthy)
fax_ingress      Up (healthy)
fax_review       Up (healthy)
fax_query        Up (healthy)
fax_worker       Up (healthy)
fax_beat         Up
```

> If `fax_ingress` shows `starting` — it is still seeding templates. Wait 2 minutes.
> Check progress: `docker logs fax_ingress --tail 20`

### Step 5 — Verify in browser

Open the Operations Console and API documentation:

- **http://localhost:8002/ui** — **Operations Console** (primary user interface)
- **http://localhost:8001/docs** — Ingress API Swagger docs
- **http://localhost:8002/docs** — Review API Swagger docs
- **http://localhost:8003/docs** — Query API Swagger docs

**The Operations Console** (`/ui`) is the recommended way to interact with the system. It provides a professional browser-based interface with five tabs:

| Tab | What You Can Do |
|-----|----------------|
| **Dashboard** | Visual analytics overview — KPI cards, processing volume chart, document type distribution, payer performance, confidence distribution |
| **Workflow** | Upload faxes (PDF/TIFF/PNG/JPEG), browse jobs, inspect results, claim and review flagged documents, submit corrections |
| **Templates** | Create/edit/delete payer templates, manage versions with matching thresholds, upload sample images, define field ROI coordinates, test template matching and extraction |
| **Intelligence** | Search extracted data (structured and semantic), run analytics reports (quality, per-payer, feedback), manage ML model versions |
| **API Console** | Execute raw API calls against any service for advanced exploration |

The console saves your connection profile (API URLs, JWT token, reviewer ID) in browser `localStorage`, so settings persist across sessions.

> **Tip:** The root URL `http://localhost:8002/` automatically redirects to the Operations Console at `/ui/`.

If you see the Operations Console interface or Swagger UI loading, the system is ready.

---

## 4. Starting & Stopping

### Start (every day)
```bash
docker compose up -d
```
Takes ~30 seconds. All data is preserved between restarts.

### Stop
```bash
docker compose down
```
Containers stop. Database, files, and templates are all preserved in Docker volumes.

### Full reset (wipes all data — use with caution)
```bash
docker compose down -v
docker compose up -d
```
This deletes all uploaded faxes, extracted data, and templates. Templates are re-seeded automatically on next start.

### View logs
```bash
docker logs fax_worker  --tail 50 --follow   # processing pipeline
docker logs fax_ingress --tail 50 --follow   # upload API
docker logs fax_review  --tail 50 --follow   # review API
```

---

## 5. Complete Workflow — Step by Step

Open **http://localhost:8001/docs** to follow along in the browser.

---

### Step A — Upload a Fax

**Endpoint:** `POST /v1/faxes/upload` on port **8001**

1. Go to **http://localhost:8001/docs**
2. Click `POST /v1/faxes/upload`
3. Click **"Try it out"** (top-right of the endpoint box)
4. Fill in the form:

| Field | What to enter | Example |
|-------|--------------|---------|
| `file` | Click "Choose File" and select a PDF fax | `humana_auth.pdf` |
| `tenant_id` | Your organisation identifier | `acme-health` |
| `payer_hint` | *(Optional)* Payer name if you already know it | `HUMANA` |

5. Click **Execute**

**Successful response (HTTP 202):**
```json
{
  "fax_job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "PENDING",
  "message": "Fax queued for processing"
}
```

> **Copy the `fax_job_id`** — you will use it in every subsequent step.

---

### Step B — Monitor Processing

**Endpoint:** `GET /v1/faxes/{fax_job_id}` on port **8001**

1. Click `GET /v1/faxes/{fax_job_id}`
2. Click **"Try it out"**
3. Paste your `fax_job_id`
4. Click **Execute**

**Processing takes 30–120 seconds** depending on document complexity.

Poll this endpoint until `status` changes from `PROCESSING`:

| Status | Meaning | Next step |
|--------|---------|-----------|
| `PENDING` | Queued, not yet started | Wait and poll again |
| `PROCESSING` | OCR + AI extraction running | Wait and poll again |
| `COMPLETED` | All fields extracted with high confidence | Go to Step C |
| `NEEDS_REVIEW` | Extracted but some fields need human verification | Go to Step C, then Step D |
| `FAILED` | Processing error | Check `docker logs fax_worker --tail 50` |

---

### Step C — Get Extraction Results

**Endpoint:** `GET /v1/faxes/{fax_job_id}/results` on port **8001**

1. Click `GET /v1/faxes/{fax_job_id}/results`
2. Click **"Try it out"**
3. Paste your `fax_job_id`
4. Click **Execute**

**Example response:**
```json
{
  "payer": "CARESOURCE",
  "doc_type": "PRIOR_AUTH_DENIAL",
  "overall_confidence": 0.94,
  "fields": {
    "patient_name":         { "value": "John Smith",    "confidence": 0.96, "source": "TEMPLATE_OCR" },
    "member_id":            { "value": "10483477800",   "confidence": 0.90, "source": "TEMPLATE_OCR" },
    "prior_auth_number":    { "value": "0806WD89S",     "confidence": 1.00, "source": "TEMPLATE_OCR" },
    "decision":             { "value": "DENIED",        "confidence": 1.00, "source": "TEMPLATE_OCR" },
    "auth_effective_date":  { "value": "08/05/2025",    "confidence": 1.00, "source": "HYBRID"       },
    "auth_expiration_date": { "value": "09/03/2025",    "confidence": 1.00, "source": "TEMPLATE_OCR" },
    "service_code":         { "value": "H2036",         "confidence": 1.00, "source": "HYBRID"       },
    "diagnosis_code":       { "value": "F84.0",         "confidence": 0.82, "source": "LAYOUTLM"     },
    "provider_name":        { "value": null,            "confidence": null, "source": null, "not_present": true }
  },
  "summary": {
    "total_fields": 15,
    "fields_found": 8,
    "fields_not_present": 7,
    "fields_flagged_for_review": 1,
    "overall_confidence": 0.94
  }
}
```

**Understanding the response:**

| Field | Meaning |
|-------|---------|
| `value` | Extracted text value. `null` = field not present in document |
| `confidence` | 0.0–1.0. Above 0.85 = high confidence |
| `source` | How the value was extracted (see table below) |
| `not_present` | `true` = field was looked for but genuinely absent from document |

**Extraction sources:**

| Source | Meaning |
|--------|---------|
| `TEMPLATE_OCR` | Matched from payer-specific form template (most reliable) |
| `OCR_LABEL` | Found by scanning OCR text for field labels |
| `LAYOUTLM` | LayoutLM AI model filled in a gap |
| `HYBRID` | Multiple sources agreed; merged result |
| `HUMAN_REVIEW` | A human reviewer corrected this field |

**If `status` is `NEEDS_REVIEW`** — some fields have confidence below threshold and were flagged. Proceed to Step D.

---

### Step D — Human Review (when `status = NEEDS_REVIEW`)

> **Recommended:** Use the **Operations Console** at **http://localhost:8002/ui** → **Workflow** tab for a visual review experience. The Review Queue section lets you browse unclaimed reviews, claim documents, view page images alongside extracted fields, and submit corrections inline — all without writing API calls.

Alternatively, you can perform the review via the Swagger API docs at **http://localhost:8002/docs**.

The review process has four steps:

---

#### D1 — List documents waiting for review

**Endpoint:** `GET /v1/faxes/review/pending` on port **8002**

1. Go to **http://localhost:8002/docs**
2. Click `GET /v1/faxes/review/pending`
3. Click **"Try it out"** → **Execute**

**Response:**
```json
[
  {
    "review_id": "a1b2c3d4-...",
    "fax_job_id": "3fa85f64-...",
    "priority": 1,
    "review_reasons": ["LOW_CONFIDENCE: member_id (0.58)", "MISSING_VALUE: diagnosis_code"],
    "claimed_by": null,
    "created_at": "2025-08-05T14:32:00Z"
  }
]
```

---

#### D2 — Claim a document

Claiming locks the document to you for 30 minutes so two reviewers do not work on the same fax simultaneously.

**Endpoint:** `POST /v1/faxes/{fax_job_id}/claim` on port **8002**

1. Click `POST /v1/faxes/{fax_job_id}/claim`
2. Click **"Try it out"**
3. Enter the `fax_job_id`
4. In the request body, enter:
```json
{
  "reviewer_id": "your-name-or-user-id"
}
```
5. Click **Execute**

**Response:**
```json
{
  "review_id": "a1b2c3d4-...",
  "claimed_by": "dr-jones",
  "claimed_at": "2025-08-05T14:35:00Z",
  "expires_at": "2025-08-05T15:05:00Z"
}
```

---

#### D3 — Open the review packet

The review packet contains everything you need to review the document:
- Presigned URLs to view each page image
- All extracted fields with confidence scores
- **Flagged fields highlighted** (the ones that need your attention)

**Endpoint:** `GET /v1/faxes/{fax_job_id}/review-packet` on port **8002**

1. Click `GET /v1/faxes/{fax_job_id}/review-packet`
2. Click **"Try it out"**
3. Enter the `fax_job_id`
4. Click **Execute**

**Response:**
```json
{
  "fax_job_id": "3fa85f64-...",
  "pages": [
    {
      "page_number": 1,
      "page_id": "...",
      "image_url": "http://localhost:9000/fax-documents/...?X-Amz-Expires=3600",
      "width_px": 1700,
      "height_px": 2200
    }
  ],
  "extracted_fields": [
    {
      "field_key": "member_id",
      "value": "10483477800",
      "confidence": 0.58,
      "is_flagged": true,
      "flag_reason": "LOW_CONFIDENCE",
      "candidates": [
        { "value": "10483477800", "method": "TEMPLATE_OCR", "confidence": 0.58 },
        { "value": "1048347780O", "method": "OCR_LABEL",    "confidence": 0.51 }
      ],
      "evidence_bbox": { "x1": 0.45, "y1": 0.23, "x2": 0.78, "y2": 0.27 }
    }
  ],
  "flagged_fields": [
    { "field_key": "member_id",     "flag_reason": "LOW_CONFIDENCE",  "current_value": "10483477800", "confidence": 0.58 },
    { "field_key": "diagnosis_code","flag_reason": "MISSING_VALUE",   "current_value": null,          "confidence": null }
  ],
  "review_reasons": ["LOW_CONFIDENCE: member_id", "MISSING_VALUE: diagnosis_code"],
  "template_match": { "payer": "CARESOURCE", "score": 0.94 },
  "created_at": "2025-08-05T14:32:00Z"
}
```

**How to review:**
1. Open the `image_url` links in your browser to see the actual fax pages
2. Look at each `flagged_field` — check if the extracted value is correct against the image
3. For flagged fields, note the correct value to submit in Step D4

---

#### D4 — Submit corrections

After reviewing the fax images, submit your corrections. You only need to include fields that you are **changing**. Leave correct fields out.

**Endpoint:** `POST /v1/faxes/{fax_job_id}/submit-review` on port **8002**

1. Click `POST /v1/faxes/{fax_job_id}/submit-review`
2. Click **"Try it out"**
3. Enter the `fax_job_id`
4. Fill in the request body with only the fields you are correcting:

```json
{
  "reviewer_id": "dr-jones",
  "corrected_fields": [
    {
      "field_key": "member_id",
      "corrected_value": "10483477800"
    },
    {
      "field_key": "diagnosis_code",
      "corrected_value": "F84.0"
    }
  ]
}
```

5. Click **Execute**

**Response:**
```json
{
  "review_id": "a1b2c3d4-...",
  "submitted_by": "dr-jones",
  "submitted_at": "2025-08-05T14:45:00Z",
  "corrections_count": 2
}
```

After submission:
- Corrected fields get `confidence = 1.0` and `source = HUMAN_REVIEW`
- The fax job status becomes `COMPLETED`
- All corrections are HIPAA-audit-logged with reviewer ID and timestamp
- The results endpoint (`GET /v1/faxes/{fax_job_id}/results`) now returns the corrected values

---

## 6. API Quick Reference

### Operations Console — http://localhost:8002/ui

The Operations Console is the primary browser interface for day-to-day operations. It covers uploading, reviewing, template management, analytics, querying, and model management — all from a single UI. See [the User Guide](docs/USER_GUIDE.md#3-operations-console-ui) for full documentation.

### Ingress API — http://localhost:8001

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/v1/faxes/upload` | Upload a fax PDF |
| `GET` | `/v1/faxes/{id}` | Get job status |
| `GET` | `/v1/faxes/{id}/results` | Get extracted fields |
| `GET` | `/v1/faxes/{id}/ocr` | Get raw OCR text |
| `DELETE` | `/v1/faxes/{id}` | Delete a fax job |
| `GET` | `/v1/faxes/` | List fax jobs |
| `GET` | `/health` | Health check |

### Review API — http://localhost:8002

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/v1/faxes/review/pending` | List documents waiting for review |
| `GET` | `/v1/faxes/review/unclaimed` | List unclaimed review documents |
| `POST` | `/v1/faxes/{id}/claim` | Claim a document for review |
| `GET` | `/v1/faxes/{id}/review-packet` | Get full review packet with flagged fields |
| `POST` | `/v1/faxes/{id}/submit-review` | Submit corrections |
| `GET` | `/v1/templates/` | List all payer templates |
| `POST` | `/v1/templates/` | Create a new template |
| `GET` | `/v1/analytics/mismatch-metrics` | Field mismatch statistics |
| `GET` | `/v1/models/` | List model versions |
| `POST` | `/v1/models/{id}/promote` | Promote a model version to active |

### Query API — http://localhost:8003

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/v1/search` | Semantic search across extracted fields |
| `GET` | `/v1/query/faxes` | Filter fax jobs by payer, status, date |

### MinIO Storage Console — http://localhost:9001

Credentials: `minioadmin` / `minioadmin123`

View uploaded fax images and page thumbnails directly in the browser.

---

## 7. Supported Payers & Fields

### Payers

| Payer | Supported Document Types |
|-------|--------------------------|
| Anthem BCBS Ohio | Prior Auth Approval, Prior Auth Denial |
| CareSource | Prior Auth Denial |
| Molina Healthcare | Prior Auth Approval, Prior Auth Denial |
| Buckeye Health Plan | Prior Auth Denial |
| Humana Healthy Horizons | Prior Auth (Inpatient) |
| UnitedHealthcare Community Plan | Prior Auth |
| AmeriHealth Caritas Ohio | Prior Auth |
| Aetna | Prior Auth |
| Paramount Advantage | Prior Auth |
| ProMedica | PA Request Form |

### Extracted Fields

| Field Key | Description | Example |
|-----------|-------------|---------|
| `patient_name` | Patient full name | `John Smith` |
| `member_id` | Insurance member ID | `10483477800` |
| `patient_dob` | Patient date of birth | `01/15/1985` |
| `prior_auth_number` | Prior authorisation reference number | `0806WD89S` |
| `auth_effective_date` | Authorisation start date | `08/05/2025` |
| `auth_expiration_date` | Authorisation end date | `09/03/2025` |
| `next_review_date` | Next scheduled review date | `09/03/2025` |
| `decision` | Auth decision | `APPROVED` or `DENIED` |
| `service_code` | CPT / HCPCS procedure code | `H2036` |
| `units_requested` | Units requested | `960` |
| `diagnosis_code` | ICD-10 diagnosis code | `F84.0` |
| `provider_name` | Rendering provider name | `Dr. Jane Doe` |
| `provider_npi` | Provider NPI number | `1234567890` |
| `provider_phone` | Provider phone | `(614) 555-1234` |
| `provider_fax` | Provider fax | `(614) 555-5678` |

---

## 8. Troubleshooting

### "This site can't be reached" on localhost:8001 / 8002 / 8003

Containers are not running. Start them:
```bash
docker compose up -d
docker ps
```

### Container keeps restarting

```bash
docker logs fax_ingress --tail 30
```
Most common cause: the database is still initialising. Wait 2 minutes and check again.

### Processing stuck in PROCESSING for more than 3 minutes

```bash
docker logs fax_worker --tail 50
```
If you see Python tracebacks, there is a processing error. Copy the error and report it.

### "No review found for job" when fetching review packet

The document status must be `NEEDS_REVIEW` before a review packet exists.
Check job status: `GET /v1/faxes/{fax_job_id}` on port 8001.

### "Already claimed" error when claiming a document

Another reviewer has claimed this document. Claims expire after 30 minutes.
Check `GET /v1/faxes/review/pending` — the `claimed_by` field shows who has it.

### Swagger UI page is blank / not loading

Open browser developer tools (F12) and check the Console tab for errors.
Make sure Docker containers are healthy: `docker ps`

### Out of disk space — Docker fails to build

Docker needs at minimum 20 GB free. Free up space:
```bash
docker builder prune --all --force   # removes build cache only (safe)
docker image prune --force           # removes unused images (safe)
```

### Reset a single container without affecting others

```bash
docker compose restart fax-worker    # restart worker only
docker compose restart fax-ingress   # restart ingress only
```

---

## 9. Production Configuration

Edit `.env.docker` before going to production:

| Variable | Development | Production |
|----------|-------------|------------|
| `ENVIRONMENT` | `development` | `production` |
| `SECRET_KEY` | `dev-secret-key-...` | **Generate a strong random key** |
| `MINIO_ACCESS_KEY` | `minioadmin` | Change to a strong value |
| `MINIO_SECRET_KEY` | `minioadmin123` | Change to a strong value |
| `API_ALLOWED_ORIGINS` | `["http://localhost:*"]` | Set to your frontend domain |

### Generate a strong SECRET_KEY

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Paste the output as the `SECRET_KEY` value in `.env.docker`.

### HTTPS / Reverse Proxy

In production, place an nginx or Traefik reverse proxy in front of ports 8001, 8002, 8003 and terminate HTTPS there. The application automatically enables HSTS and strict security headers when `ENVIRONMENT=production`.

### Authentication

In development mode (`ENVIRONMENT=development`), all requests run as an admin user with no token required — convenient for testing.

In production mode (`ENVIRONMENT=production`), every request must include a JWT Bearer token in the `Authorization` header. Contact your system administrator for token issuance setup.

### HIPAA Audit Log

All access to patient data (upload, results, review) is recorded in the `audit_log` database table with:
- User ID and tenant ID
- Action type (READ, DOWNLOAD, CORRECTION)
- Timestamp
- Resource identifier

To query audit logs:
```bash
docker exec fax_postgres psql -U faxadmin -d fax_processor \
  -c "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 20;"
```

---

*System version 1.0.0 — All rights reserved*
