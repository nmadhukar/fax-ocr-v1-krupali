# REST API Reference — Healthcare Fax OCR Processing System

Complete endpoint reference for all three FastAPI services. All endpoints require JWT authentication unless noted otherwise.

> **Operations Console:** For a visual interface to all API operations, open `http://localhost:8002/ui` in your browser. The console covers upload, review, template management, analytics, query, and model management — no API calls required. See the [User Guide](USER_GUIDE.md#3-operations-console-ui) for details.

---

## Table of Contents

1. [Authentication](#1-authentication)
2. [Fax Ingress API (Port 8001)](#2-fax-ingress-api-port-8001)
3. [Fax Review API (Port 8002)](#3-fax-review-api-port-8002)
4. [Fax Query API (Port 8003)](#4-fax-query-api-port-8003)
5. [Common Response Patterns](#5-common-response-patterns)
6. [Error Codes Reference](#6-error-codes-reference)
7. [Pydantic Schema Definitions](#7-pydantic-schema-definitions)

---

## 1. Authentication

All endpoints require a JWT Bearer token in the `Authorization` header:

```
Authorization: Bearer <jwt_token>
```

### Token Structure

The JWT payload contains:
```json
{
  "sub": "user_id",
  "tenant_id": "tenant_001",
  "is_admin": false,
  "exp": 1700000000
}
```

### Development Mode

When `ENVIRONMENT=development`, tenant isolation checks are relaxed. The system still requires a valid JWT but won't enforce tenant boundary restrictions.

---

## 2. Fax Ingress API (Port 8001)

**Base URL:** `http://localhost:8001`
**Swagger UI:** `http://localhost:8001/docs` (when `API_DEBUG=true`)

### 2.1 Health Check

```
GET /health
```

**Authentication:** None required

**Response:** `200 OK`
```json
{
  "status": "healthy",
  "service": "fax-ingress"
}
```

---

### 2.2 Upload Fax

```
POST /v1/faxes/upload
```

**Content-Type:** `multipart/form-data`

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file` | File | Yes | Fax file (PDF, TIFF, PNG, JPEG). Max 50MB. |
| `tenant_id` | string | Yes | Tenant identifier |
| `payer_hint` | string | No | Optional payer hint (e.g., "ANTHEM", "UNITED_HEALTH") |
| `external_fax_id` | string | No | Optional external reference ID |

**Allowed file types:** `application/pdf`, `image/tiff`, `image/tif`, `image/png`, `image/jpeg`, `image/jpg`

**Response:** `201 Created`
```json
{
  "fax_job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PENDING",
  "created_at": "2025-01-15T10:30:00Z",
  "message": "Fax uploaded successfully and queued for processing"
}
```

**Duplicate Detection:** If the same file (by SHA-256 hash) is uploaded again for the same tenant, returns `200 OK` with the existing job:
```json
{
  "fax_job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "COMPLETED",
  "created_at": "2025-01-15T10:30:00Z",
  "message": "Duplicate fax detected. Returning existing job."
}
```

**Error Responses:**
- `400` — Invalid file type, file too large, or magic-byte validation failure
- `403` — Tenant mismatch (non-admin uploading to different tenant)
- `429` — Rate limit exceeded
- `500` — Storage failure
- `503` — Processing queue unavailable

**cURL Example:**
```bash
curl -X POST http://localhost:8001/v1/faxes/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/fax.pdf" \
  -F "tenant_id=tenant_001" \
  -F "payer_hint=ANTHEM"
```

---

### 2.3 Get Fax Job Status

```
GET /v1/faxes/{fax_job_id}
```

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `fax_job_id` | UUID | Fax job identifier |

**Response:** `200 OK`
```json
{
  "fax_job_id": "550e8400-e29b-41d4-a716-446655440000",
  "tenant_id": "tenant_001",
  "original_filename": "anthem_pa_form.pdf",
  "status": "COMPLETED",
  "total_pages": 3,
  "payer_hint": "ANTHEM",
  "doc_type": "PRIOR_AUTH_APPROVAL",
  "overall_conf": 0.9234,
  "needs_review": false,
  "created_at": "2025-01-15T10:30:00Z",
  "processing_started_at": "2025-01-15T10:30:05Z",
  "processing_completed_at": "2025-01-15T10:30:45Z"
}
```

**Status Values:** `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`, `NEEDS_REVIEW`

---

### 2.4 Get Extraction Results

```
GET /v1/faxes/{fax_job_id}/results
```

Returns clean, client-facing extraction results. Only available when status is `COMPLETED` or `NEEDS_REVIEW`.

**Response:** `200 OK`
```json
{
  "fax_job_id": "550e8400-e29b-41d4-a716-446655440000",
  "payer": "ANTHEM",
  "doc_type": "PRIOR_AUTH_APPROVAL",
  "status": "COMPLETED",
  "overall_confidence": 0.9234,
  "needs_review": false,
  "fields": {
    "patient_name": {
      "value": "Jane Doe",
      "confidence": 0.95,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "member_id": {
      "value": "MBR001234",
      "confidence": 0.98,
      "source": "HYBRID",
      "not_present": false
    },
    "prior_auth_number": {
      "value": "PA-00123",
      "confidence": 0.90,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "auth_effective_date": {
      "value": "2025-01-15",
      "confidence": 1.0,
      "source": "HYBRID",
      "not_present": false
    },
    "auth_expiration_date": {
      "value": "2025-03-15",
      "confidence": 1.0,
      "source": "HYBRID",
      "not_present": false
    },
    "decision": {
      "value": "APPROVED",
      "confidence": 0.90,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "units_requested": {
      "value": null,
      "confidence": 0.0,
      "source": "HYBRID",
      "not_present": true
    }
  },
  "summary": {
    "total_fields": 7,
    "fields_found": 6,
    "fields_not_present": 1,
    "fields_flagged_for_review": 0,
    "flagged_field_keys": []
  }
}
```

**Error Responses:**
- `404` — Job not found or no extraction results
- `409` — Results not available (job still processing)

---

### 2.5 Get Raw OCR Data

```
GET /v1/faxes/{fax_job_id}/ocr
```

Returns full OCR token data for all pages.

**Response:** `200 OK`
```json
{
  "fax_job_id": "550e8400-e29b-41d4-a716-446655440000",
  "original_filename": "anthem_pa_form.pdf",
  "status": "COMPLETED",
  "total_pages": 3,
  "pages": [
    {
      "page_number": 1,
      "width_px": 2550,
      "height_px": 3300,
      "full_text": "Anthem Blue Cross Blue Shield\nPrior Authorization...",
      "tokens": [
        {
          "token_text": "Anthem",
          "line_number": 1,
          "word_number": 1,
          "confidence": 0.98,
          "bbox": {"x0": 0.05, "y0": 0.02, "x1": 0.25, "y1": 0.04}
        }
      ]
    }
  ]
}
```

---

### 2.6 List Fax Jobs

```
GET /v1/faxes
```

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tenant_id` | string | null | Filter by tenant (admin only) |
| `status_filter` | string | null | Filter by status (PENDING, COMPLETED, etc.) |
| `skip` | int | 0 | Pagination offset |
| `limit` | int | 100 | Max results (capped at 500) |

**Response:** `200 OK`
```json
{
  "faxes": [ /* array of FaxJobResponse */ ],
  "total": 42,
  "skip": 0,
  "limit": 100
}
```

---

### 2.7 Delete Fax Job

```
DELETE /v1/faxes/{fax_job_id}
```

Deletes the job, associated files from MinIO, and all related database records (cascading).

**Response:** `204 No Content`

---

## 3. Fax Review API (Port 8002)

**Base URL:** `http://localhost:8002`
**Operations Console:** `http://localhost:8002/ui` (always available)
**Swagger UI:** `http://localhost:8002/docs` (when `API_DEBUG=true`)

---

### 3.1 Template Management

#### Create Template

```
POST /v1/templates
```

**Admin only.** Creates a new template for a payer + document type combination.

**Request Body:**
```json
{
  "payer_name": "ANTHEM",
  "doc_type": "PRIOR_AUTH_FORM",
  "template_name": "Anthem Standard PA Form",
  "description": "Standard prior authorization form used by Anthem"
}
```

**Response:** `201 Created`
```json
{
  "template_id": "uuid-here",
  "payer_name": "ANTHEM",
  "doc_type": "PRIOR_AUTH_FORM",
  "template_name": "Anthem Standard PA Form",
  "description": "Standard prior authorization form used by Anthem",
  "is_active": true,
  "created_at": "2025-01-15T10:00:00Z"
}
```

#### List Templates

```
GET /v1/templates?payer_name=ANTHEM&active_only=false
```

#### Get Template

```
GET /v1/templates/{template_id}
```

#### Update Template

```
PUT /v1/templates/{template_id}
```

**Request Body:**
```json
{
  "template_name": "Updated Name",
  "description": "Updated description",
  "is_active": false
}
```

#### Delete Template

```
DELETE /v1/templates/{template_id}
```

Cascades to all versions, samples, and fields.

---

### 3.2 Template Version Management

#### Create Version

```
POST /v1/templates/{template_id}/versions
```

**Request Body:**
```json
{
  "version_label": "v1.0",
  "match_min_score": 0.75,
  "match_phash_threshold": 10,
  "match_orb_min_matches": 20
}
```

#### Activate Version

```
POST /v1/templates/versions/{version_id}/activate
```

#### Update Version Thresholds

```
PUT /v1/templates/versions/{version_id}
```

**Request Body:**
```json
{
  "match_min_score": 0.80,
  "match_phash_threshold": 8,
  "match_orb_min_matches": 25
}
```

---

### 3.3 Template Sample Management

#### Upload Sample Image

```
POST /v1/templates/versions/{version_id}/samples
```

**Content-Type:** `multipart/form-data`

| Parameter | Type | Description |
|-----------|------|-------------|
| `file` | File | Sample page image (PNG, JPEG, TIFF, BMP). Max 10MB. |

The system automatically computes pHash and ORB features from the uploaded image.

**Response:** `201 Created`
```json
{
  "sample_id": "uuid-here",
  "sample_storage_key": "templates/version-id/20250115T..._sample.png",
  "width_px": 2550,
  "height_px": 3300,
  "created_at": "2025-01-15T10:00:00Z"
}
```

---

### 3.4 Template Field Definition

#### Create Field

```
POST /v1/templates/versions/{version_id}/fields
```

**Request Body:**
```json
{
  "field_key": "member_id",
  "field_label": "Member ID",
  "is_required": true,
  "roi_x0": 0.05,
  "roi_y0": 0.15,
  "roi_x1": 0.45,
  "roi_y1": 0.19,
  "target_page": 1,
  "validation_regex": "^[A-Z0-9]{8,14}$",
  "expected_type": "text"
}
```

**ROI coordinates** are normalized 0.0–1.0 relative to page dimensions.

#### List Fields

```
GET /v1/templates/versions/{version_id}/fields
```

---

### 3.5 Template Testing

#### Test Match

```
POST /v1/templates/test-match
```

**Content-Type:** `multipart/form-data`

| Parameter | Type | Description |
|-----------|------|-------------|
| `file` | File | Image to test against templates. Max 10MB. |
| `payer_hint` | string | Optional payer filter |

**Response:**
```json
{
  "matched": true,
  "template_version_id": "uuid-here",
  "template_name": "Anthem Standard PA Form",
  "payer_name": "ANTHEM",
  "match_score": 0.87,
  "phash_distance": 3,
  "orb_matches": 45,
  "orb_inliers": 38
}
```

#### Test Extract

```
POST /v1/templates/versions/{version_id}/test-extract
```

**Content-Type:** `multipart/form-data`

| Parameter | Type | Description |
|-----------|------|-------------|
| `file` | File | Page image for extraction test. Max 10MB. |

**Response:**
```json
{
  "version_id": "uuid-here",
  "fields": [
    {
      "field_key": "member_id",
      "value": "MBR001234",
      "confidence": 0.95,
      "evidence_bbox": {"x0": 0.05, "y0": 0.15, "x1": 0.45, "y1": 0.19, "page": 1},
      "evidence_text": "MBR001234"
    }
  ],
  "total_fields": 5
}
```

#### Suggest ROI

```
POST /v1/templates/suggest-roi
```

**Request Body:**
```json
{
  "fax_job_id": "uuid-of-processed-fax",
  "page_number": 1,
  "field_key": "member_id",
  "correct_value": "MBR001234"
}
```

**Response:**
```json
{
  "suggested_roi": {"x0": 0.04, "y0": 0.14, "x1": 0.46, "y1": 0.20},
  "evidence_text": "MBR001234",
  "confidence": 0.92
}
```

---

### 3.6 Review Workflow

#### Get Review Packet

```
GET /v1/faxes/{fax_job_id}/review-packet
```

Returns the complete review context including page images (presigned URLs), extracted fields with candidates, and HITL flags.

**Response:** `200 OK`
```json
{
  "fax_job_id": "uuid-here",
  "pages": [
    {
      "page_number": 1,
      "page_id": "uuid-here",
      "image_url": "http://minio:9000/fax-documents/...?X-Amz-Signature=...",
      "width_px": 2550,
      "height_px": 3300
    }
  ],
  "extracted_fields": [
    {
      "field_key": "member_id",
      "value": "MBR001234",
      "confidence": 0.72,
      "evidence_bbox": {"page": 1, "x0": 0.1, "y0": 0.2, "x1": 0.4, "y1": 0.25},
      "candidates": [
        {"value": "MBR001234", "method": "TEMPLATE_OCR", "confidence": 0.72},
        {"value": "MBR00I234", "method": "LAYOUTLM", "confidence": 0.65}
      ],
      "is_flagged": true,
      "flag_reason": "LOW_CONFIDENCE"
    }
  ],
  "template_match": {"template_name": "Anthem Standard PA Form", "score": 0.87},
  "review_reasons": ["Overall confidence below threshold", "2 fields flagged for review"],
  "created_at": "2025-01-15T10:30:45Z",
  "flagged_fields": [
    {"field_key": "member_id", "flag_reason": "LOW_CONFIDENCE", "confidence": 0.72, "threshold": 0.85}
  ]
}
```

#### Claim Review

```
POST /v1/faxes/{fax_job_id}/review/claim
```

Claims a review for the authenticated user. Uses optimistic locking to prevent concurrent claims.

**Request Body:**
```json
{
  "reviewer_id": null
}
```

**Response:** `200 OK`
```json
{
  "review_id": "uuid-here",
  "claimed_by": "user_123",
  "claimed_at": "2025-01-15T11:00:00Z",
  "expires_at": "2025-01-15T11:30:00Z"
}
```

**Error Responses:**
- `404` — No review found for this job
- `400` — Review already submitted
- `409` — Review already claimed by another reviewer

#### Submit Review

```
POST /v1/faxes/{fax_job_id}/review/submit
```

Submits corrections. Each correction is validated against an allowlist of known field keys.

**Request Body:**
```json
{
  "reviewer_id": null,
  "corrected_fields": [
    {
      "field_key": "member_id",
      "corrected_value": "MBR001234",
      "evidence_bbox": {"page": 1, "x0": 0.1, "y0": 0.2, "x1": 0.4, "y1": 0.25}
    },
    {
      "field_key": "auth_effective_date",
      "corrected_value": "2025-01-15",
      "evidence_bbox": null
    }
  ]
}
```

**Response:** `200 OK`
```json
{
  "review_id": "uuid-here",
  "submitted_by": "user_123",
  "submitted_at": "2025-01-15T11:15:00Z",
  "corrections_count": 2
}
```

**Side Effects:**
1. Corrections applied to `extraction_json` with `method=HUMAN_REVIEW`, `confidence=1.0`
2. Feedback records created in `fax_feedback` table
3. Training labels created in `fax_label_example` table
4. Job status updated to `COMPLETED`, `needs_review=false`

#### List Pending Reviews

```
GET /v1/faxes/reviews/pending?skip=0&limit=100
```

#### List Unclaimed Reviews

```
GET /v1/faxes/reviews/unclaimed?skip=0&limit=100
```

#### Release Expired Claims (Admin Only)

```
POST /v1/faxes/reviews/release-expired
```

**Response:**
```json
{
  "released_count": 3
}
```

---

### 3.7 Analytics

#### Quality Metrics

```
GET /v1/analytics/quality?days=30
```

**Admin only.** Returns overall processing quality metrics.

**Response:**
```json
{
  "total_jobs": 1250,
  "completed_jobs": 1180,
  "failed_jobs": 15,
  "needs_review_jobs": 55,
  "auto_finalize_rate": 0.944,
  "avg_confidence": 0.891,
  "processing_times": {
    "avg_seconds": 32.5,
    "p50_seconds": 28.0,
    "p95_seconds": 65.0
  },
  "doc_type_distribution": {
    "PRIOR_AUTH_APPROVAL": 820,
    "PRIOR_AUTH_DENIAL": 150,
    "PRIOR_AUTH_FORM": 200,
    "OTHER": 10
  }
}
```

#### Per-Payer Stats

```
GET /v1/analytics/payer/ANTHEM?days=30
```

#### All Payer Comparison

```
GET /v1/analytics/payers?days=30
```

#### Feedback Summary

```
GET /v1/analytics/feedback-summary?days=30
```

#### Trigger Recalibration

```
POST /v1/analytics/recalibrate?days=30
```

**Admin only.** Analyzes correction history and returns per-payer, per-field confidence threshold recommendations.

---

### 3.8 Model Version Management

#### List Model Versions

```
GET /v1/models?model_type=LAYOUTLM
```

#### Get Active Version

```
GET /v1/models/active/LAYOUTLM
```

#### Register Model Version

```
POST /v1/models
```

**Request Body:**
```json
{
  "model_type": "LAYOUTLM",
  "version_tag": "layoutlm-v1.1",
  "model_path": "/app/.cache/adapters/layoutlm-v1.1",
  "notes": "Fine-tuned on 500 corrections",
  "config": {"learning_rate": 2e-5, "epochs": 3}
}
```

#### Promote Model Version

```
POST /v1/models/{model_version_id}/promote
```

Deactivates all other versions of the same type and activates this one.

#### Update Model Metrics

```
PUT /v1/models/{model_version_id}/metrics
```

**Request Body:**
```json
{
  "metrics": {
    "accuracy": 0.92,
    "f1_score": 0.89,
    "avg_latency_ms": 1200,
    "eval_samples": 200
  }
}
```

#### Delete Model Version

```
DELETE /v1/models/{model_version_id}
```

---

## 4. Fax Query API (Port 8003)

**Base URL:** `http://localhost:8003`

### 4.1 Query Fax Data

```
POST /v1/query
```

**Request Body:**
```json
{
  "query": "member_id",
  "tier": 1,
  "limit": 10,
  "fax_job_id": null
}
```

**Tier Options:**

| Tier | Name | Method |
|------|------|--------|
| 1 | Structured | SQL ILIKE search on field keys and values |
| 2 | Semantic | pgvector cosine similarity on embeddings |
| 3 | Summariser | Not implemented (returns 501) |

**Response:** `200 OK`
```json
{
  "query_id": "uuid-here",
  "query": "member_id",
  "tier": 1,
  "results": [
    {
      "fax_job_id": "uuid-here",
      "fax_page_id": null,
      "field_key": "member_id",
      "value": "MBR001234",
      "confidence": 0.95,
      "method": "TEMPLATE_OCR",
      "source_type": "field",
      "similarity_score": null
    }
  ],
  "total_results": 1,
  "latency_ms": 12.5
}
```

---

## 5. Common Response Patterns

### Pagination

List endpoints support pagination with `skip` and `limit`:
```
GET /v1/faxes?skip=20&limit=10
```

The `limit` parameter is capped at 500 to prevent accidental bulk data dumps.

### Tenant Isolation

- Non-admin users can only access their own tenant's data
- Admin users can access all tenants or filter by `tenant_id`
- Every request is scoped to the authenticated user's `tenant_id`

### Audit Logging

Every data-access endpoint writes an audit record:
- `log_read` — viewing data
- `log_create` — creating records
- `log_delete` — deleting records
- `log_download` — accessing page images
- `log_review_claim` / `log_review_submit` — review workflow

---

## 6. Error Codes Reference

| HTTP Code | Meaning | Common Causes |
|-----------|---------|---------------|
| `400` | Bad Request | Invalid file type, missing fields, validation failure |
| `401` | Unauthorized | Missing or invalid JWT token |
| `403` | Forbidden | Tenant mismatch, admin-only endpoint |
| `404` | Not Found | Job/template/review not found |
| `409` | Conflict | Results not ready, review already claimed, concurrent update |
| `429` | Too Many Requests | Upload rate limit exceeded |
| `500` | Internal Error | Storage failure, database error |
| `501` | Not Implemented | Tier 3 query (summariser stub) |
| `503` | Service Unavailable | Processing queue unavailable |

### Error Response Format

```json
{
  "detail": "Human-readable error message"
}
```

---

## 7. Pydantic Schema Definitions

### Enums

**PayerNameEnum:** `ANTHEM`, `UNITED_HEALTH`, `HUMANA`, `MOLINA`, `CARESOURCE`, `BUCKEYE`, `AMERIHEALTH`, `AETNA`, `PARAMOUNT`, `PROMEDICA`, `UNKNOWN`

**DocTypeEnum:** `PRIOR_AUTH_FORM`, `PRIOR_AUTH_APPROVAL`, `PRIOR_AUTH_DENIAL`, `PEER_TO_PEER_DENIAL`, `FAX_COVER_SHEET`, `HIPAA_RELEASE`, `CLINICAL_NOTES`, `LAB_RESULTS`, `OTHER`, `UNKNOWN`

**FaxJobStatusEnum:** `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`, `NEEDS_REVIEW`

**ExtractionMethodEnum:** `TEMPLATE_OCR`, `OCR_LABEL`, `LAYOUTLM`, `VLM`, `HUMAN_REVIEW`, `HYBRID`, `LLM`

### Client-Facing Field Format

Every extracted field in the results API follows this structure:

```json
{
  "value": "string or null",
  "confidence": 0.0,
  "source": "TEMPLATE_OCR | LAYOUTLM | HYBRID | HUMAN_REVIEW | VLM | UNKNOWN",
  "not_present": false
}
```

- `value` is `null` when `not_present=true`
- `confidence` ranges from 0.0 to 1.0 (rounded to 4 decimal places)
- `source` indicates which extraction method produced the winning value
- `not_present=true` means the field was not found in the document
