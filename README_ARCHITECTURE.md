# System Architecture

This document describes the complete architecture of the Healthcare Fax Processing System: how components are organized, how data flows through each stage of the pipeline, and how the system makes decisions at each step.

---

## High-Level Overview

The system is built as a set of loosely coupled services that communicate through a message queue. A fax PDF enters through the Ingress API, gets queued for processing, passes through a 17-stage pipeline in a worker process, and emerges as structured data accessible through multiple APIs.

```
         CLIENT APPLICATION
         (EMR Systems, Fax Gateways)
               |
       HTTP (REST/JSON)
               |
    +----------+-----------+
    |                       |
 Ingress API            Review API            Query API
 (port 8001)            (port 8002)           (port 8003)
    |                    |     |
    |                    |     +-- Operations Console UI (/ui)
    |  Celery task queue |        (HTML + CSS + vanilla JS)
    |  (Redis)           |  Read/write
    v                    |
 Processing Worker  -----+
    |
    | Reads/writes
    v
 PostgreSQL  +  MinIO  +  Redis
 (database)    (files)   (cache)
```

The three APIs share a single PostgreSQL database and MinIO file store. The worker is the only service that runs the heavy processing — OCR, template matching, and model inference. The APIs are lightweight and handle only request routing, authentication, and database reads/writes.

---

## Infrastructure Components

### PostgreSQL (pgvector/pgvector:pg15)

Stores all structured data: job records, page metadata, OCR tokens, template definitions, extraction results, review feedback, audit logs, and model version registry.

The pgvector extension enables vector similarity search, used by the Query API for semantic document search.

### Redis (redis:7)

Serves two purposes:
- **Task queue broker**: Celery uses Redis to receive and distribute processing jobs from the Ingress API to the worker.
- **Result backend**: Celery writes task results to Redis so the API can check whether a job has been picked up.

Redis data is persisted to disk via append-only log (`appendonly yes`) so tasks survive a Redis restart.

### MinIO

Object storage for binary files — uploaded PDFs and rendered page images. MinIO is API-compatible with Amazon S3, so the client code uses standard S3 patterns.

Two buckets are created on startup:
- `fax-documents` — uploaded PDFs and rendered page images
- `templates` — template sample images and LayoutLM adapter files

---

## Service Components

### Fax Ingress API (port 8001)

Responsibilities:
- Accept fax PDF uploads, validate file type and size
- Store the PDF in MinIO
- Create a `fax_job` database record with status `PENDING`
- Publish a Celery task to the processing queue
- Return job status and extraction results to callers

This service has no processing logic. It delegates all work to the worker via the queue.

### Fax Review API (port 8002)

Responsibilities:
- **Serve the Operations Console UI** — A browser-based SPA at `/ui` that consolidates all system workflows (upload, review, templates, analytics, query, model management) into a single professional interface. Built with vanilla HTML/CSS/JS (zero dependencies, no build step).
- Serve the review queue (jobs flagged for human attention)
- Accept reviewer corrections and apply them back to extraction data
- Provide template management (CRUD for payer form templates)
- Provide analytics on extraction accuracy and volume
- Manage LayoutLM model version registry

### Fax Query API (port 8003)

A read-optimized API for downstream integration:
- Query extraction results by job ID or search parameters
- Optimized for high-frequency polling by EHR systems or automation pipelines
- No write operations — read-only

### Processing Worker (Celery)

The core of the system. Picks up jobs from the Redis queue and runs the full 17-stage pipeline. The worker is stateful — it keeps the LayoutLM model loaded in memory between jobs to avoid reload overhead.

### Celery Beat Scheduler

Runs scheduled tasks on a timer:
- Weekly LayoutLM retraining (when enough new labeled examples have accumulated)
- Hourly mismatch metric aggregation

---

## Processing Pipeline

Every fax goes through the same sequence of stages. The worker processes stages in order, writing intermediate results to the database after each stage. If a stage fails, the job is marked `FAILED` and the reason is logged.

### Stage 1 — Receive and Validate

The worker picks up the Celery task. It loads the `fax_job` record from the database and verifies the PDF exists in MinIO. The job status is updated to `PROCESSING`.

### Stage 2 — PDF to Pages

The PDF is downloaded from MinIO and split into individual pages using `pdf2image` (a poppler wrapper). Each page is rendered at 300 DPI to a PNG image. Pages are stored back to MinIO and a `fax_page` record is created for each.

300 DPI is non-negotiable — the perceptual hashing for template matching was computed at 300 DPI, so documents must be processed at the same resolution to produce matching hashes.

### Stage 3a — Image Preprocessing

Each page image is passed through a preprocessing pipeline in `libs/shared/utils/image_utils.py`:

1. **Deskew**: Rotates the image to correct page tilt caused by physical fax misalignment. Uses Hough line transform to detect text line angle.
2. **Denoise**: Applies morphological opening to remove speckle noise common in fax transmissions.
3. **Binarize**: Converts to black-and-white using adaptive thresholding, which handles pages with uneven lighting.
4. **Normalize contrast**: Stretches the histogram to ensure OCR sees maximum contrast.

The preprocessed image is saved back to MinIO alongside the original.

### Stage 3b — Cover Page Detection

Cover pages (fax transmission headers, confidentiality notices) contain recipient names and other data that would contaminate extraction if treated as content. Two detection passes run:

**Pass 1 — Pixel density check**: A page with fewer than 1% of pixels containing text is likely a blank separator page and is excluded.

**Pass 2 — OCR keyword check**: After OCR runs (stage 4), the full text of each page is scanned for cover-page signals. A weighted scoring system is applied:

| Pattern | Weight |
|---|---|
| "fax cover sheet" | 2.0 |
| "facsimile transmission" | 1.5 |
| "cover sheet" | 1.5 |
| "confidentiality notice" | 1.0 |
| "To:" at line start | 0.5 |
| "From:" at line start | 0.5 |
| "Subject:" at line start | 0.5 |
| "Pages:" at line start | 0.5 |

If the accumulated score is 1.5 or higher AND the page contains no authorization-specific keywords (member ID, prior auth, CPT code, etc.), the page is marked as a cover page and excluded from all subsequent extraction.

### Stage 4 — OCR

PaddleOCR (PP-OCRv5) runs on each non-cover page. OCR is deliberately isolated in a subprocess to prevent PaddleOCR's native library from interfering with SQLAlchemy connection pooling — a known compatibility issue on Windows.

Output: a list of OCR tokens per page, each with:
- The recognized text string
- A bounding box (normalized x0, y0, x1, y1 coordinates, 0.0 to 1.0)
- A per-token confidence score

Tokens are stored in the `fax_ocr_token` table. The full concatenated page text is also stored for fast scanning.

### Stage 5 — Payer Detection

The system identifies which insurance company sent the form. This determines which template and field definitions to use for extraction.

**Method: Perceptual Hashing (pHash)**

Each page's preprocessed image is hashed using a difference hash (dHash) at 300 DPI. The resulting 64-bit integer is compared against hashes stored for all known payer templates.

Hamming distance measures similarity: a distance of 10 or less (out of 64 bits) is considered a match. The payer with the lowest distance is selected.

pHash values in the database are stored as signed BigInteger (PostgreSQL `bigint`) using a two's-complement conversion. The application converts between signed and unsigned representations when reading/writing.

**Fallback: OCR keyword scan**

If no pHash match is found (distance > 10 for all templates), the system scans the full document OCR text for payer-specific keywords. Each payer has a weighted keyword set in `configs/payer_rules.yml`. The payer with the highest keyword score is selected.

If no payer scores above threshold, the job continues with `payer = UNKNOWN` and template matching is skipped.

### Stage 6 — Template Matching

Given a detected payer, the system finds the best-matching template version. This is a two-step verification:

**Step 1 — pHash match**: Confirms that the document's visual hash is within Hamming distance 10 of the template's stored hash.

**Step 2 — Anchor verification**: The system looks for specific anchor text strings defined in the template (e.g., "Prior Authorization Request", "Member Information"). The proportion of anchors found in the OCR output is the anchor score. A score below 0.40 causes the match to be rejected even if pHash matched.

This double verification prevents cross-payer contamination — e.g., two payers whose forms happen to have similar visual hash values.

The matched template version determines which field definitions to use in the next stage.

### Stage 7 — Document Classification

Identifies the document type: prior auth request, approval letter, denial, clinical notes, etc. Uses a keyword-based classifier in `libs/shared/classification/document_splitter.py`.

The document type influences which fields are considered mandatory and which HITL confidence thresholds apply.

### Stage 8 — Template Field Extraction

The primary extraction method. For each field defined in the matched template, the extractor searches the OCR token list using a label-anchored strategy.

**Label-anchored extraction** (primary):
1. Search for the field's label text among OCR tokens (e.g., "Member ID:", "Authorization #:")
2. Once the label token is located, collect tokens immediately to the right of the label (inline value), then tokens directly below if no inline value was found
3. Apply field-type-specific validation and trimming:
   - Auth numbers: strip common prefixes ("PA-", "REF:", etc.)
   - Dates: extract from ranges ("08/01/2025 – 09/01/2025" → "08/01/2025")
   - Service codes: per-word check, minimum 4 characters, alphanumeric format
   - Phone/fax/NPI: reject if not matching expected pattern (no fallback)
4. Score the candidate based on position match quality and validation result

**ROI fallback**: If no label is found, fall back to extracting all tokens within the defined region of interest (x0, y0, x1, y1 bounding box), then apply the same validation.

Each extracted candidate includes the source token text, bounding box, confidence score, and validation status.

### Stage 9 — OCR Label Extraction

A second independent extraction pass using regex-based label matching. This runs in parallel with stage 8.

The OCR label extractor (`libs/shared/extraction/ocr_label_extractor.py`) uses a set of label patterns per field that cover variations across payer formats (e.g., "Member ID", "Medicaid ID", "Subscriber ID", "Plan ID" all map to `member_id`).

For each pattern match, the extractor collects the value using the same right-of-label and below-label logic as the template extractor. Results are independent candidates that will be merged in stage 11.

### Stage 10 — LayoutLM Gap-Fill

If any fields still have no candidates (or have low-confidence candidates) after stages 8 and 9, the LayoutLM Document QA model is used to fill the gaps.

LayoutLM (`impira/layoutlm-document-qa`) is a transformer model pre-trained for document question answering. For each missing field, it is asked a targeted question in natural language:

- "What is the member ID number?"
- "What is the authorization number or reference number?"
- "What is the patient date of birth?"

Three question variants are asked per field to maximize recall. Results are deduplicated, keeping the highest-scoring answer per unique text. The model returns extractive answers — the answer text is always a real span from the document.

The model receives the page image and a list of OCR tokens with normalized (0–1000) bounding boxes. This gives it both visual and textual context.

**Cap**: LayoutLM is only called on up to 8 fields per job, with a 90-second timeout per field, to prevent slow documents from blocking the worker indefinitely.

### Stage 10d — Heuristic Safety Net

A set of post-extraction heuristics correct common errors before merging:

- **next_review_date mis-classified as patient_dob**: Dates more than 18 years in the past are valid DOBs. If a `next_review_date` candidate contains a date more than 18 years ago, it is reclassified as `patient_dob`.
- **Buckeye narrative format**: Buckeye documents use narrative text rather than structured fields. A dedicated scan extracts patient name and auth number from the narrative.
- **Fax cover contamination**: If `patient_name` contains recipient-style text ("ATTN:", "Following member:"), it is discarded.
- **Auth number prefix cleanup**: Removes noise prefixes ("Reference#:", "Auth#:", etc.) from extracted auth numbers when LayoutLM is the source.

### Stage 11 — Multi-Source Merge

Candidates from all three extraction sources (template, OCR label, LayoutLM) are merged in `libs/shared/extraction/field_builder.py`.

For each field, the merger:
1. Collects all candidates from all sources
2. Scores each candidate:
   - Template OCR candidates start at their template extraction score
   - OCR label candidates have a base score with a validation bonus
   - LayoutLM candidates have their model confidence score, multiplied by a configurable bonus multiplier (default 0.70x for base model, 1.20x for fine-tuned adapter)
   - A contamination penalty is applied if the candidate value appears on another field's likely value list
3. Selects the highest-scoring candidate as the winning value
4. If two sources produced the same value, the method is recorded as `HYBRID` and a small confidence bonus is applied

### Stage 12 — Confidence Scoring

`libs/shared/monitoring/pipeline_metrics.py` computes an overall confidence score for the job by averaging field-level confidence scores. Critical fields (patient_name, member_id, auth dates, decision, service_code, diagnosis_code) are weighted more heavily.

### Stage 13 — Validation

Field values are re-validated against field-type rules:
- NPI: must be exactly 10 digits
- Phone/fax: must match a phone number pattern
- Dates: must be parseable as a date
- Service codes: must be alphanumeric, minimum 4 characters
- ICD codes: must match ICD-10 format

Fields that fail validation have their confidence reduced by 0.30.

### Stage 14 — NOT_PRESENT Classification

Any field with a winning candidate confidence below 0.30 is classified as not present in the document (`not_present = true`, `value = null`). This threshold indicates that no meaningful evidence was found across all extraction sources.

### Stage 15 — HITL Flagging

Fields that passed the 0.30 threshold but are below a higher review threshold are flagged for human attention:

- **Critical fields** (patient_name, member_id, auth_effective_date, auth_expiration_date, decision, service_code, diagnosis_code): flagged if confidence < 0.85
- **Other fields**: flagged if confidence < 0.75

Flagged fields are stored in the `fax_extraction.flagged_fields` JSONB column. If one or more fields are flagged, the job status is set to `NEEDS_REVIEW`.

Fields already corrected by a human (`source = HUMAN_REVIEW`) are never flagged — they are considered authoritative regardless of the original confidence score.

### Stage 16 — Finalization

The extraction results are written to the `fax_extraction` table. The job status is set to:
- `COMPLETED` — if no fields are flagged and overall confidence is above the auto-finalize threshold (default 0.90)
- `NEEDS_REVIEW` — if any fields are flagged or overall confidence is below the needs-review threshold (default 0.65)
- `COMPLETED` — for jobs with confidence between the two thresholds (auto-finalized with moderate confidence)

### Stage 17 — Output Formatting

The extraction data is stored internally in a rich format that includes candidates, evidence text, bounding boxes, and validation state. This internal format is kept in the database for audit and retraining purposes.

When a client calls `GET /v1/faxes/{id}/results`, the output formatter (`libs/shared/extraction/output_formatter.py`) transforms the internal format into the clean client-facing structure: one value per field, with confidence, source, and not_present flag.

---

## Human-in-the-Loop Feedback Loop

The review system creates a continuous improvement cycle:

```
Processing pipeline produces extraction
           |
           | (some fields below threshold)
           v
    NEEDS_REVIEW status
           |
    Reviewer opens job
           |
    Reviewer corrects wrong fields
           |
    Corrections saved:
     - confidence = 1.0
     - source = HUMAN_REVIEW
     - original value archived in candidates[]
     - correction written to fax_label_example table
           |
    When accumulated corrections >= RETRAIN_MIN_NEW_LABELS (default 20):
           |
    Weekly Celery Beat task triggers:
     - generate_training_data.py exports labeled examples
     - finetune_layoutlm.py trains a LoRA adapter
     - New adapter registered in model_version table
     - Admin promotes new version to production
           |
    Worker loads new adapter on next restart
           |
    Fewer fields fall below threshold
           |
    Fewer NEEDS_REVIEW cases
```

This loop means the system improves over time based on real corrections, specifically tuning the LayoutLM model to your document population and payer set.

---

## Data Storage Schema

### Core Tables

| Table | Contents |
|---|---|
| `fax_job` | One record per uploaded fax: status, payer, tenant, timestamps |
| `fax_page` | One record per page: page number, is_cover_page, image storage key |
| `fax_ocr_token` | All OCR tokens for all pages: text, bounding box, confidence |
| `fax_template` | Payer template definitions |
| `fax_template_version` | Versioned template configurations with pHash values |
| `fax_template_field` | Field definitions per template version: ROI, label text, field type |
| `fax_extraction` | Extraction results: extraction_json (full internal), flagged_fields |
| `fax_review_feedback` | Review outcomes: corrections, reviewer identity, timestamps |
| `fax_label_example` | Training examples for LayoutLM fine-tuning |
| `model_version` | Registered LayoutLM adapter versions |
| `audit_log` | HIPAA-required log of all PHI access |
| `mismatch_metric` | Hourly aggregation of extraction source disagreements |

### Key JSONB Columns

**`fax_extraction.extraction_json`**: Internal pipeline format per field. Stored as a JSON object keyed by field name. Each field contains:
```json
{
  "value": "MBR001234",
  "confidence": 0.98,
  "method": "HYBRID",
  "evidence_bbox": {"x0": 0.12, "y0": 0.31, "x1": 0.41, "y1": 0.35},
  "evidence_text": "Member ID: MBR001234",
  "candidates": [
    {"value": "MBR001234", "confidence": 0.98, "method": "TEMPLATE_OCR"},
    {"value": "MBR001234", "confidence": 0.95, "method": "LAYOUTLM"}
  ],
  "validation_passed": true,
  "validation_errors": [],
  "not_present": false
}
```

**`fax_extraction.flagged_fields`**: Array of HITL flags:
```json
[
  {"field_key": "member_id", "reason": "LOW_CONFIDENCE", "threshold": 0.85, "confidence": 0.61}
]
```

---

## Multi-Tenancy

All data is scoped to a tenant. The `fax_job` table has a `tenant_id` column. Every authenticated API request carries the user's tenant_id in their JWT claims.

At the repository layer, all queries filter by `tenant_id` unless the requesting user has admin role. This means a user from Tenant A cannot read, modify, or detect the existence of Tenant B's jobs, regardless of the API endpoint called.

Admin users can query across all tenants and are identified by an `is_admin` flag in their JWT claims.

---

## Security Model

**Authentication**: All endpoints require a JWT Bearer token. Tokens are signed with `SECRET_KEY` using HS256.

**Authorization**: Tenant isolation is enforced at the database query level (not just in the route handler).

**HIPAA Audit Logging**: Every operation that reads or writes patient data (upload, status, results, review packet, corrections) is logged to the `audit_log` table with: user identity, tenant, action, resource ID, timestamp, and outcome.

**Rate Limiting**: Token-bucket algorithm with background cleanup. Limits are per authenticated user. A background daemon thread runs every 5 minutes to remove expired bucket entries.

**File Validation**: Uploads are validated for MIME type (PDF only), file size, and basic structural integrity before being stored.

**Credential Enforcement**: When `ENVIRONMENT=production`, the application raises a `RuntimeError` at startup if `SECRET_KEY`, `MINIO_ACCESS_KEY`, or `MINIO_SECRET_KEY` contain their default development values.

---

## Scaling Considerations

The worker is the processing bottleneck. Each worker process handles one job at a time (LayoutLM and PaddleOCR are CPU-intensive and not safely parallelizable within a single process). To increase throughput:

- Run multiple worker containers: `docker compose scale fax-worker=3`
- The `--concurrency=2` flag in the current Compose file allows two parallel Celery tasks per worker container. Reduce this to 1 if memory is constrained.
- The APIs and database can handle much higher load than the worker — scale the worker first.

LayoutLM adds approximately 15-30 seconds of processing per job on CPU. If this is too slow, a GPU-equipped machine will bring it down to under 5 seconds. Set `VLM_USE_GPU=true` and use a CUDA-enabled base image.
