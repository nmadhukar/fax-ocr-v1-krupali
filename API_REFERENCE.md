# Healthcare Fax OCR — Complete API Reference

> **Operations Console:** A professional browser UI at `http://localhost:8002/ui` provides a unified interface for all API operations — upload, review, templates, analytics, query, and model management.
>
> All three APIs also expose interactive Swagger documentation at `/docs` on their respective ports.
> In development mode (`ENVIRONMENT=development`) no authentication token is required.

**Base URLs:**
- Operations Console: `http://localhost:8002/ui`
- Ingress API: `http://localhost:8001`
- Review API:  `http://localhost:8002`
- Query API:   `http://localhost:8003`

---

## Table of Contents

- [Ingress API (port 8001)](#ingress-api--port-8001)
  - [Upload a fax](#post-v1faxesupload)
  - [Get job status](#get-v1faxesfax_job_id)
  - [Get extraction results](#get-v1faxesfax_job_idresults)
  - [Get raw OCR text](#get-v1faxesfax_job_idocr)
  - [List fax jobs](#get-v1faxes)
  - [Delete a fax job](#delete-v1faxesfax_job_id)
  - [Health check](#get-health-ingress)
- [Review API (port 8002)](#review-api--port-8002)
  - [List pending reviews](#get-v1faxesreviewspending)
  - [List unclaimed reviews](#get-v1faxesreviewsunclaimed)
  - [Claim a document](#post-v1faxesfax_job_idreviewclaim)
  - [Get review packet](#get-v1faxesfax_job_idreview-packet)
  - [Submit corrections](#post-v1faxesfax_job_idreviewsubmit)
  - [Release expired claims](#post-v1faxesreviewsrelease-expired)
  - [List templates](#get-v1templates)
  - [Analytics — quality metrics](#get-v1analyticsquality)
  - [Analytics — per-payer stats](#get-v1analyticspayerpayer_name)
  - [Model versions](#get-v1models)
  - [Promote a model](#post-v1modelsmodel_version_idpromote)
- [Query API (port 8003)](#query-api--port-8003)
  - [Search / query](#post-v1query)

---

## Ingress API — Port 8001

---

### `POST /v1/faxes/upload`

Upload a healthcare fax PDF for processing.

**Request** — multipart/form-data

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | File (PDF) | Yes | The fax document. Max 50 MB. PDF only. |
| `tenant_id` | string | Yes | Your organisation identifier (e.g. `acme-health`) |
| `payer_hint` | string | No | Optional payer name if known. Values: `ANTHEM`, `CARESOURCE`, `MOLINA`, `BUCKEYE`, `HUMANA`, `UNITEDHEALTHCARE`, `AMERIHEALTH`, `AETNA`, `PARAMOUNT`, `PROMEDICA` |

**curl example:**
```bash
curl -X POST http://localhost:8001/v1/faxes/upload \
  -F "file=@/path/to/fax.pdf" \
  -F "tenant_id=acme-health" \
  -F "payer_hint=HUMANA"
```

**Response 201:**
```json
{
  "fax_job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "PENDING",
  "message": "Fax queued for processing",
  "tenant_id": "acme-health",
  "created_at": "2025-08-05T14:30:00Z"
}
```

**Error responses:**

| Code | Meaning |
|------|---------|
| 400 | File is not a valid PDF |
| 413 | File exceeds 50 MB limit |
| 422 | Missing required field |
| 503 | Processing queue unavailable |

---

### `GET /v1/faxes/{fax_job_id}`

Get the current status of a fax job.

**Path parameter:** `fax_job_id` — UUID from the upload response

**curl example:**
```bash
curl http://localhost:8001/v1/faxes/3fa85f64-5717-4562-b3fc-2c963f66afa6
```

**Response 200:**
```json
{
  "fax_job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "tenant_id": "acme-health",
  "status": "NEEDS_REVIEW",
  "payer": "HUMANA",
  "doc_type": "PRIOR_AUTH_APPROVAL",
  "page_count": 2,
  "overall_confidence": 0.82,
  "created_at": "2025-08-05T14:30:00Z",
  "completed_at": "2025-08-05T14:31:45Z"
}
```

**Status values:**

| Status | Meaning |
|--------|---------|
| `PENDING` | Job queued, not yet started |
| `PROCESSING` | OCR + AI extraction in progress (30–120 s) |
| `COMPLETED` | All fields extracted with sufficient confidence |
| `NEEDS_REVIEW` | Extracted but ≥1 field flagged for human verification |
| `FAILED` | Processing error — check `docker logs fax_worker` |

---

### `GET /v1/faxes/{fax_job_id}/results`

Get the full structured extraction results for a completed fax job.
Available once status is `COMPLETED` or `NEEDS_REVIEW`.

**curl example:**
```bash
curl http://localhost:8001/v1/faxes/3fa85f64-5717-4562-b3fc-2c963f66afa6/results
```

**Response 200:**
```json
{
  "fax_job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "payer": "CARESOURCE",
  "doc_type": "PRIOR_AUTH_DENIAL",
  "overall_confidence": 0.94,
  "fields": {
    "patient_name": {
      "value": "John Smith",
      "confidence": 0.96,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "member_id": {
      "value": "10483477800",
      "confidence": 0.90,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "prior_auth_number": {
      "value": "0806WD89S",
      "confidence": 1.00,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "decision": {
      "value": "DENIED",
      "confidence": 1.00,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "auth_effective_date": {
      "value": "08/05/2025",
      "confidence": 1.00,
      "source": "HYBRID",
      "not_present": false
    },
    "auth_expiration_date": {
      "value": "09/03/2025",
      "confidence": 1.00,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "service_code": {
      "value": "H2036",
      "confidence": 1.00,
      "source": "HYBRID",
      "not_present": false
    },
    "diagnosis_code": {
      "value": "F84.0",
      "confidence": 0.82,
      "source": "LAYOUTLM",
      "not_present": false
    },
    "provider_name": {
      "value": null,
      "confidence": null,
      "source": null,
      "not_present": true
    }
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

**Field object properties:**

| Property | Type | Description |
|----------|------|-------------|
| `value` | string \| null | Extracted text. `null` if field not found or not present in document |
| `confidence` | float \| null | 0.0–1.0. Higher = more reliable |
| `source` | string \| null | Extraction method (see below) |
| `not_present` | bool | `true` = field searched for but definitively absent from document |

**Source values:**

| Source | Description |
|--------|-------------|
| `TEMPLATE_OCR` | Extracted using payer-specific template + OCR (most reliable) |
| `OCR_LABEL` | Found by scanning OCR text for field labels |
| `LAYOUTLM` | LayoutLM AI model filled in a gap |
| `HYBRID` | Multiple sources agreed; values merged |
| `HUMAN_REVIEW` | Value was corrected by a human reviewer |

---

### `GET /v1/faxes/{fax_job_id}/ocr`

Get the raw OCR output for a fax job (useful for debugging extraction issues).

**curl example:**
```bash
curl http://localhost:8001/v1/faxes/3fa85f64-5717-4562-b3fc-2c963f66afa6/ocr
```

**Response 200:**
```json
{
  "fax_job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "pages": [
    {
      "page_number": 1,
      "tokens": [
        {
          "text": "CareSource",
          "bbox": { "x1": 0.05, "y1": 0.03, "x2": 0.25, "y2": 0.07 },
          "confidence": 0.99
        }
      ],
      "full_text": "CareSource\nPrior Authorization Notice\nMember ID: 10483477800\n..."
    }
  ]
}
```

---

### `GET /v1/faxes/`

List fax jobs with optional filters.

**Query parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `tenant_id` | string | Filter by tenant (admin sees all if omitted) |
| `status` | string | Filter by status: `PENDING`, `PROCESSING`, `COMPLETED`, `NEEDS_REVIEW`, `FAILED` |
| `payer` | string | Filter by payer name |
| `limit` | int | Max results (default: 20, max: 100) |
| `offset` | int | Pagination offset (default: 0) |

**curl example:**
```bash
curl "http://localhost:8001/v1/faxes/?status=NEEDS_REVIEW&limit=10"
```

**Response 200:**
```json
{
  "items": [
    {
      "fax_job_id": "3fa85f64-...",
      "tenant_id": "acme-health",
      "status": "NEEDS_REVIEW",
      "payer": "HUMANA",
      "overall_confidence": 0.78,
      "created_at": "2025-08-05T14:30:00Z"
    }
  ],
  "total": 1,
  "limit": 10,
  "offset": 0
}
```

---

### `DELETE /v1/faxes/{fax_job_id}`

Delete a fax job and all associated data (pages, extractions, review records).

**curl example:**
```bash
curl -X DELETE http://localhost:8001/v1/faxes/3fa85f64-5717-4562-b3fc-2c963f66afa6
```

**Response:** `204 No Content`

---

### `GET /health` (Ingress)

Health check — verifies database, Redis, and MinIO connectivity.

```bash
curl http://localhost:8001/health
```

**Response 200 (healthy):**
```json
{
  "status": "healthy",
  "service": "fax-ingress",
  "version": "1.0.0",
  "checks": {
    "database": "ok",
    "redis": "ok",
    "storage": "ok"
  }
}
```

---

## Review API — Port 8002

---

### `GET /v1/faxes/reviews/pending`

List all fax jobs currently in the review queue (claimed and unclaimed).

```bash
curl http://localhost:8002/v1/faxes/reviews/pending
```

**Response 200:**
```json
[
  {
    "review_id": "a1b2c3d4-1234-5678-abcd-ef0123456789",
    "fax_job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "priority": 1,
    "review_reasons": [
      "LOW_CONFIDENCE: member_id (0.58)",
      "MISSING_VALUE: diagnosis_code"
    ],
    "claimed_by": null,
    "created_at": "2025-08-05T14:32:00Z"
  }
]
```

**`priority` field:** 1 = highest priority (critical fields flagged), 5 = low priority

---

### `GET /v1/faxes/reviews/unclaimed`

List only unclaimed (available) review documents — ready to be picked up.

```bash
curl http://localhost:8002/v1/faxes/reviews/unclaimed
```

Response format is identical to `/reviews/pending` but only shows items where `claimed_by` is `null`.

---

### `POST /v1/faxes/{fax_job_id}/review/claim`

Claim a document for review. Locks the document to the specified reviewer for 30 minutes.
Only one reviewer can hold a claim at a time.

**Request body:**
```json
{
  "reviewer_id": "dr-jones"
}
```

**curl example:**
```bash
curl -X POST http://localhost:8002/v1/faxes/3fa85f64-5717-4562-b3fc-2c963f66afa6/review/claim \
  -H "Content-Type: application/json" \
  -d '{"reviewer_id": "dr-jones"}'
```

**Response 200:**
```json
{
  "review_id": "a1b2c3d4-1234-5678-abcd-ef0123456789",
  "claimed_by": "dr-jones",
  "claimed_at": "2025-08-05T14:35:00Z",
  "expires_at": "2025-08-05T15:05:00Z"
}
```

**Error 409:** Document is already claimed by another reviewer and the claim has not expired.

---

### `GET /v1/faxes/{fax_job_id}/review-packet`

Get the complete review packet for a claimed document. Contains:
- Presigned image URLs for all pages (valid 1 hour)
- All extracted fields with confidence scores and candidates
- Highlighted flagged fields that need attention

```bash
curl http://localhost:8002/v1/faxes/3fa85f64-5717-4562-b3fc-2c963f66afa6/review-packet
```

**Response 200:**
```json
{
  "fax_job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "pages": [
    {
      "page_number": 1,
      "page_id": "page-uuid-here",
      "image_url": "http://localhost:9000/fax-documents/jobs/3fa85f64.../page_1.png?X-Amz-Expires=3600&...",
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
    },
    {
      "field_key": "diagnosis_code",
      "value": null,
      "confidence": null,
      "is_flagged": true,
      "flag_reason": "MISSING_VALUE",
      "candidates": [],
      "evidence_bbox": null
    },
    {
      "field_key": "prior_auth_number",
      "value": "0806WD89S",
      "confidence": 1.00,
      "is_flagged": false,
      "flag_reason": null,
      "candidates": [
        { "value": "0806WD89S", "method": "TEMPLATE_OCR", "confidence": 1.00 }
      ],
      "evidence_bbox": { "x1": 0.45, "y1": 0.15, "x2": 0.75, "y2": 0.19 }
    }
  ],
  "flagged_fields": [
    {
      "field_key": "member_id",
      "flag_reason": "LOW_CONFIDENCE",
      "current_value": "10483477800",
      "confidence": 0.58
    },
    {
      "field_key": "diagnosis_code",
      "flag_reason": "MISSING_VALUE",
      "current_value": null,
      "confidence": null
    }
  ],
  "review_reasons": [
    "LOW_CONFIDENCE: member_id (0.58)",
    "MISSING_VALUE: diagnosis_code"
  ],
  "template_match": {
    "payer": "CARESOURCE",
    "score": 0.94,
    "template_name": "CareSource PA Denial v1"
  },
  "created_at": "2025-08-05T14:32:00Z"
}
```

**How to use the `image_url`:**
Copy the URL from `pages[n].image_url` and paste it into your browser.
It opens the actual fax page image so you can verify extracted values against the real document.

**`evidence_bbox` coordinates** are normalised (0.0–1.0) relative to page dimensions.
They indicate where on the page the value was found.

**`flag_reason` values:**

| Value | Meaning |
|-------|---------|
| `LOW_CONFIDENCE` | Extraction confidence is below the threshold for this field type |
| `MISSING_VALUE` | Field expected but no value found in the document |

---

### `POST /v1/faxes/{fax_job_id}/review/submit`

Submit corrections after reviewing the document. Include only fields you are changing.
Fields not included in `corrected_fields` keep their current extracted values unchanged.

**Request body:**
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

**curl example:**
```bash
curl -X POST http://localhost:8002/v1/faxes/3fa85f64-5717-4562-b3fc-2c963f66afa6/review/submit \
  -H "Content-Type: application/json" \
  -d '{
    "reviewer_id": "dr-jones",
    "corrected_fields": [
      {"field_key": "member_id", "corrected_value": "10483477800"},
      {"field_key": "diagnosis_code", "corrected_value": "F84.0"}
    ]
  }'
```

**Response 200:**
```json
{
  "review_id": "a1b2c3d4-1234-5678-abcd-ef0123456789",
  "submitted_by": "dr-jones",
  "submitted_at": "2025-08-05T14:45:00Z",
  "corrections_count": 2
}
```

**After submission:**
- Corrected fields get `confidence = 1.0` and `source = HUMAN_REVIEW`
- The fax job status changes to `COMPLETED`
- All corrections are HIPAA-audit-logged
- `GET /v1/faxes/{id}/results` on port 8001 now returns corrected values

---

### `POST /v1/faxes/reviews/release-expired`

Admin utility — release all claims that have passed their 30-minute expiry.
Run this if reviewers abandon documents without submitting.

```bash
curl -X POST http://localhost:8002/v1/faxes/reviews/release-expired
```

**Response 200:**
```json
{ "released": 3 }
```

---

### `GET /v1/templates/`

List all seeded payer templates with their fields and current version.

```bash
curl http://localhost:8002/v1/templates/
```

**Response 200:**
```json
[
  {
    "template_id": "tmpl-uuid-...",
    "payer_name": "CARESOURCE",
    "template_name": "CareSource PA Denial",
    "active_version": "v1",
    "field_count": 7,
    "created_at": "2025-08-01T00:00:00Z"
  }
]
```

---

### `GET /v1/analytics/quality`

Overall extraction quality metrics across all processed documents.

```bash
curl http://localhost:8002/v1/analytics/quality
```

**Response 200:**
```json
{
  "total_jobs": 150,
  "completed": 142,
  "needs_review": 8,
  "failed": 0,
  "average_confidence": 0.91,
  "fields_auto_completed_pct": 94.7,
  "fields_human_corrected_pct": 5.3
}
```

---

### `GET /v1/analytics/payer/{payer_name}`

Accuracy metrics broken down for a specific payer.

```bash
curl http://localhost:8002/v1/analytics/payer/HUMANA
```

**Response 200:**
```json
{
  "payer": "HUMANA",
  "total_jobs": 32,
  "average_confidence": 0.93,
  "field_mismatch_rate": 0.04,
  "most_flagged_field": "auth_expiration_date"
}
```

---

### `GET /v1/models/`

List all registered AI model versions.

```bash
curl http://localhost:8002/v1/models/
```

**Response 200:**
```json
[
  {
    "model_version_id": "mv-uuid-...",
    "model_type": "LAYOUTLM",
    "version": "layoutlm-v1.0",
    "is_active": true,
    "accuracy": 0.94,
    "registered_at": "2025-08-01T00:00:00Z"
  }
]
```

---

### `POST /v1/models/{model_version_id}/promote`

Promote a model version to active (deactivates the current active version for that model type).

```bash
curl -X POST http://localhost:8002/v1/models/mv-uuid-here/promote
```

**Response 200:** Returns the updated model version object with `is_active: true`.

---

## Query API — Port 8003

---

### `POST /v1/query`

Search across all extracted fields using either structured SQL lookup (Tier 1) or semantic vector search (Tier 2).

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | Search text or question |
| `tenant_id` | string | No | Filter to a specific tenant (default: `default`) |
| `tier` | int (1–2) | No | 1 = structured SQL (fast, exact), 2 = semantic vector (slower, fuzzy). Default: 1 |
| `limit` | int | No | Max results 1–100 (default: 10) |
| `fax_job_id` | string | No | Restrict search to a specific fax job |

**Tier 1 — Structured lookup:** finds exact or partial field value matches in the database.

```bash
curl -X POST http://localhost:8003/v1/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "member_id 10483477800",
    "tier": 1,
    "limit": 5
  }'
```

**Tier 2 — Semantic search:** uses vector embeddings to find conceptually similar documents.

```bash
curl -X POST http://localhost:8003/v1/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "denied prior authorization for applied behavior analysis",
    "tier": 2,
    "limit": 5
  }'
```

**Response 200:**
```json
{
  "query_id": "q-uuid-...",
  "query": "member_id 10483477800",
  "tier": 1,
  "results": [
    {
      "fax_job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "fax_page_id": null,
      "field_key": "member_id",
      "value": "10483477800",
      "confidence": 0.90,
      "method": "TEMPLATE_OCR",
      "source_type": "field",
      "similarity_score": null
    }
  ],
  "total_results": 1,
  "processing_time_ms": 12
}
```

---

## Common Error Responses

All APIs return errors in this format:

```json
{
  "detail": "Fax job not found: 3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

| HTTP Code | Meaning |
|-----------|---------|
| `400` | Bad request — invalid input (e.g. not a PDF) |
| `401` | Unauthorised — missing or invalid JWT token (production only) |
| `403` | Forbidden — you do not have access to this resource |
| `404` | Resource not found |
| `409` | Conflict — e.g. document already claimed |
| `413` | File too large (max 50 MB) |
| `422` | Validation error — check the `detail` field for specifics |
| `503` | Service unavailable — processing queue down |

---

## Field Keys Reference

Use these exact strings in `field_key` when submitting review corrections:

| Key | Description |
|-----|-------------|
| `patient_name` | Patient full name |
| `member_id` | Insurance member / subscriber ID |
| `patient_dob` | Patient date of birth (MM/DD/YYYY) |
| `prior_auth_number` | Prior authorisation reference number |
| `auth_effective_date` | Authorisation start date (MM/DD/YYYY) |
| `auth_expiration_date` | Authorisation end / expiry date (MM/DD/YYYY) |
| `next_review_date` | Next scheduled review date (MM/DD/YYYY) |
| `decision` | Auth decision: `APPROVED` or `DENIED` |
| `service_code` | CPT or HCPCS procedure code (e.g. `H2036`) |
| `units_requested` | Number of units requested |
| `diagnosis_code` | ICD-10 diagnosis code (e.g. `F84.0`) |
| `provider_name` | Rendering provider full name |
| `provider_npi` | Provider NPI (10-digit number) |
| `provider_phone` | Provider phone number |
| `provider_fax` | Provider fax number |
