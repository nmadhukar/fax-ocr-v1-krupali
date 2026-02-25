# API Reference

Complete reference for all three APIs: Ingress (port 8001), Review (port 8002), and Query (port 8003).

All endpoints require a Bearer token in the `Authorization` header unless otherwise noted.

---

## Authentication

### Login

```
POST /v1/auth/login
Content-Type: application/json
```

Request body:
```json
{
  "username": "string",
  "password": "string"
}
```

Response `200 OK`:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

Response `401 Unauthorized`:
```json
{
  "detail": "Invalid credentials"
}
```

Use the `access_token` in all subsequent requests:
```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

---

## Ingress API — Port 8001

Handles fax uploads and result retrieval.

### Health Check

```
GET /health
```
No authentication required.

Response `200`:
```json
{ "status": "ok" }
```

---

### Upload a Fax

```
POST /v1/faxes/upload
Content-Type: multipart/form-data
Authorization: Bearer <token>
```

Form fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | File | Yes | PDF file. Max size set by `API_MAX_UPLOAD_SIZE_MB` (default 50 MB) |
| `tenant_id` | string | Yes | Your organization's tenant identifier |
| `payer_hint` | string | No | Payer name to speed up matching. One of: `ANTHEM`, `CARESOURCE`, `MOLINA`, `BUCKEYE`, `HUMANA`, `UNITED_HEALTH`, `AMERIHEALTH`, `AETNA`, `PARAMOUNT`, `PROMEDICA` |

Response `202 Accepted`:
```json
{
  "fax_job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PENDING",
  "tenant_id": "demo_tenant",
  "created_at": "2025-08-01T10:30:00.000Z",
  "message": "Fax queued for processing"
}
```

Response `400 Bad Request` (invalid file):
```json
{
  "detail": "Uploaded file must be a PDF"
}
```

Response `503 Service Unavailable` (worker queue failure):
```json
{
  "detail": "Processing queue unavailable. Please retry."
}
```

Processing happens asynchronously. Poll the status endpoint to track progress.

---

### Get Job Status

```
GET /v1/faxes/{fax_job_id}
Authorization: Bearer <token>
```

Path parameter:
- `fax_job_id` — UUID returned from the upload response

Response `200 OK`:
```json
{
  "fax_job_id": "550e8400-e29b-41d4-a716-446655440000",
  "tenant_id": "demo_tenant",
  "status": "COMPLETED",
  "payer": "ANTHEM",
  "doc_type": "PRIOR_AUTH_FORM",
  "overall_confidence": 0.912,
  "needs_review": false,
  "page_count": 3,
  "created_at": "2025-08-01T10:30:00.000Z",
  "completed_at": "2025-08-01T10:30:45.000Z"
}
```

**Status values:**

| Status | Meaning |
|---|---|
| `PENDING` | Job is in the queue, not yet started |
| `PROCESSING` | Pipeline is actively running |
| `COMPLETED` | All fields extracted with sufficient confidence |
| `NEEDS_REVIEW` | One or more fields are flagged for human review |
| `FAILED` | Processing failed (check logs) |

Response `404 Not Found`:
```json
{
  "detail": "Fax job not found"
}
```

Response `403 Forbidden`:
```json
{
  "detail": "Access denied"
}
```
Non-admin users can only access jobs belonging to their own tenant.

---

### Get Extracted Results

Returns the final structured extraction in client-ready format. Only available when status is `COMPLETED` or `NEEDS_REVIEW`.

```
GET /v1/faxes/{fax_job_id}/results
Authorization: Bearer <token>
```

Response `200 OK`:
```json
{
  "fax_job_id": "550e8400-e29b-41d4-a716-446655440000",
  "tenant_id": "demo_tenant",
  "payer": "ANTHEM",
  "doc_type": "PRIOR_AUTH_FORM",
  "status": "COMPLETED",
  "overall_confidence": 0.912,
  "needs_review": false,
  "fields": {
    "patient_name": {
      "value": "Jane Doe",
      "confidence": 0.97,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "member_id": {
      "value": "MBR001234567",
      "confidence": 0.98,
      "source": "HYBRID",
      "not_present": false
    },
    "prior_auth_number": {
      "value": "PA-00123456",
      "confidence": 0.90,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "patient_dob": {
      "value": "01/15/1980",
      "confidence": 0.95,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "auth_effective_date": {
      "value": "08/07/2025",
      "confidence": 1.00,
      "source": "HYBRID",
      "not_present": false
    },
    "auth_expiration_date": {
      "value": "09/05/2025",
      "confidence": 1.00,
      "source": "HYBRID",
      "not_present": false
    },
    "next_review_date": {
      "value": null,
      "confidence": 0.00,
      "source": "HYBRID",
      "not_present": true
    },
    "provider_name": {
      "value": "General Hospital",
      "confidence": 0.88,
      "source": "LAYOUTLM",
      "not_present": false
    },
    "provider_npi": {
      "value": "1234567890",
      "confidence": 0.93,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "provider_phone": {
      "value": "(555) 123-4567",
      "confidence": 0.91,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "provider_fax": {
      "value": "(555) 987-6543",
      "confidence": 0.89,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "service_code": {
      "value": "H2012",
      "confidence": 0.94,
      "source": "HYBRID",
      "not_present": false
    },
    "units_requested": {
      "value": "96",
      "confidence": 0.87,
      "source": "TEMPLATE_OCR",
      "not_present": false
    },
    "diagnosis_code": {
      "value": "F32.1",
      "confidence": 0.92,
      "source": "HYBRID",
      "not_present": false
    },
    "decision": {
      "value": "APPROVED",
      "confidence": 0.90,
      "source": "TEMPLATE_OCR",
      "not_present": false
    }
  },
  "summary": {
    "total_fields": 15,
    "fields_found": 14,
    "fields_not_present": 1,
    "fields_flagged_for_review": 0,
    "flagged_field_keys": []
  }
}
```

**Field object schema:**

| Key | Type | Description |
|---|---|---|
| `value` | string or null | The extracted value. `null` when `not_present` is true |
| `confidence` | float 0.0–1.0 | Extraction confidence score |
| `source` | string | Which extraction method produced this value |
| `not_present` | boolean | True when the field does not appear in the document |

**Source values:**

| Source | Meaning |
|---|---|
| `TEMPLATE_OCR` | Extracted using label-anchored template matching against OCR tokens |
| `OCR_LABEL` | Found by scanning OCR text for field labels |
| `LAYOUTLM` | Extracted by the LayoutLM Document QA model |
| `HYBRID` | Multiple extraction sources agreed; highest confidence value used |
| `DONUT` | Extracted by Donut end-to-end model |
| `HUMAN_REVIEW` | Manually corrected by a human reviewer — treat as authoritative |
| `HUMAN` | Manually entered by a human operator |
| `SYSTEM` | Metadata-sourced field (payer name, fax received date) — not OCR-extracted |
| `UNKNOWN` | Legacy or fallback — source could not be determined |

**Extractable fields (18 total):**

| Field Key | Description | Format | Source |
|---|---|---|---|
| `payer_name` | Insurance company name | Payer enum value | SYSTEM |
| `member_id` | Health plan member ID | Alphanumeric | OCR |
| `patient_name` | Patient/member full name | Free text | OCR |
| `patient_dob` | Patient date of birth | MM/DD/YYYY | OCR |
| `decision` | Authorization decision | APPROVED / DENIED / PENDING | OCR |
| `prior_auth_number` | Prior authorization reference number | Alphanumeric | OCR |
| `service_code` | HCPCS or CPT service code | Alphanumeric | OCR |
| `units_requested` | Units or days requested/approved | Numeric string | OCR |
| `auth_effective_date` | Authorization start date (From Date) | MM/DD/YYYY | OCR |
| `auth_expiration_date` | Authorization end date (To Date) | MM/DD/YYYY | OCR |
| `insurance_rep_name` | Insurance representative who approved/denied | Free text | OCR |
| `insurance_rep_phone` | Insurance representative contact number | Phone format | OCR |
| `fax_received_date` | Date and time fax was received | MM/DD/YYYY HH:MM | SYSTEM |
| `next_review_date` | Next clinical review date | MM/DD/YYYY | OCR |
| `provider_name` | Treating provider or facility name | Free text | OCR |
| `provider_npi` | National Provider Identifier | 10-digit number | OCR |
| `provider_phone` | Provider phone number | Phone format | OCR |
| `provider_fax` | Provider fax number | Phone format | OCR |
| `diagnosis_codes` | ICD-10 diagnosis code | ICD-10 format | OCR |

Response `404`: Job not found
Response `409`: Job not yet complete (status is PENDING or PROCESSING)

---

### List Jobs

```
GET /v1/faxes
Authorization: Bearer <token>
```

Query parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `status_filter` | string | none | Filter by status: `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`, `NEEDS_REVIEW` |
| `payer_filter` | string | none | Filter by payer name |
| `limit` | integer | 50 | Results per page (max 200) |
| `offset` | integer | 0 | Pagination offset |

Admin users see all tenants. Non-admin users see only their own tenant's jobs.

Response `200 OK`:
```json
{
  "total": 142,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "fax_job_id": "550e8400-...",
      "tenant_id": "demo_tenant",
      "status": "COMPLETED",
      "payer": "ANTHEM",
      "doc_type": "PRIOR_AUTH_FORM",
      "overall_confidence": 0.912,
      "needs_review": false,
      "created_at": "2025-08-01T10:30:00.000Z",
      "completed_at": "2025-08-01T10:30:45.000Z"
    }
  ]
}
```

---

### Get Raw OCR Output

Returns the raw OCR token data for all pages of a job. Useful for debugging extraction results.

```
GET /v1/faxes/{fax_job_id}/ocr
Authorization: Bearer <token>
```

Response `200 OK`:
```json
{
  "fax_job_id": "550e8400-...",
  "pages": [
    {
      "page_number": 1,
      "is_cover_page": false,
      "token_count": 347,
      "tokens": [
        {
          "text": "AUTHORIZATION",
          "x0": 0.12,
          "y0": 0.08,
          "x1": 0.38,
          "y1": 0.11,
          "confidence": 0.99
        }
      ]
    }
  ]
}
```

Token coordinates are normalized to the range 0.0–1.0 relative to page dimensions.

---

### Delete a Job

Permanently removes the job record and all associated files from storage.

```
DELETE /v1/faxes/{fax_job_id}
Authorization: Bearer <token>
```

Response `204 No Content` — success, no body returned.

---

## Review API — Port 8002

Handles the human review workflow, template management, analytics, and model version tracking.

### Get Jobs Pending Review

```
GET /v1/faxes/pending-review
Authorization: Bearer <token>
```

Returns jobs in `NEEDS_REVIEW` status that have not yet been claimed by a reviewer.

Response `200 OK`:
```json
{
  "total": 5,
  "items": [
    {
      "fax_job_id": "550e8400-...",
      "tenant_id": "demo_tenant",
      "payer": "ANTHEM",
      "doc_type": "PRIOR_AUTH_FORM",
      "overall_confidence": 0.68,
      "flagged_field_count": 3,
      "created_at": "2025-08-01T10:30:00.000Z"
    }
  ]
}
```

---

### Get Review Packet

Returns the complete document for review: page images, all extracted fields with flag status, and metadata needed for a reviewer to assess the document.

```
GET /v1/faxes/{fax_job_id}/review-packet
Authorization: Bearer <token>
```

Response `200 OK`:
```json
{
  "fax_job_id": "550e8400-...",
  "tenant_id": "demo_tenant",
  "payer": "ANTHEM",
  "doc_type": "PRIOR_AUTH_FORM",
  "overall_confidence": 0.68,
  "flagged_fields": [
    {
      "field_key": "member_id",
      "reason": "LOW_CONFIDENCE",
      "threshold": 0.85,
      "confidence": 0.61
    },
    {
      "field_key": "service_code",
      "reason": "LOW_CONFIDENCE",
      "threshold": 0.85,
      "confidence": 0.54
    }
  ],
  "fields": {
    "patient_name": {
      "value": "Jane Doe",
      "confidence": 0.97,
      "source": "TEMPLATE_OCR",
      "not_present": false,
      "is_flagged": false,
      "flag_reason": null
    },
    "member_id": {
      "value": "MBR001234",
      "confidence": 0.61,
      "source": "LAYOUTLM",
      "not_present": false,
      "is_flagged": true,
      "flag_reason": "LOW_CONFIDENCE"
    },
    "service_code": {
      "value": "H201",
      "confidence": 0.54,
      "source": "LAYOUTLM",
      "not_present": false,
      "is_flagged": true,
      "flag_reason": "LOW_CONFIDENCE"
    }
  },
  "pages": [
    {
      "page_number": 1,
      "is_cover_page": false,
      "image_url": "http://localhost:9000/fax-documents/pages/550e8400-.../page_001.png",
      "width_px": 2550,
      "height_px": 3300
    }
  ]
}
```

**Flag reasons:**

| Reason | Description |
|---|---|
| `LOW_CONFIDENCE` | Extraction confidence is below the field's threshold |
| `MISSING_VALUE` | Field is expected for this document type but has no value |

---

### Claim a Job for Review

Locks the job to a specific reviewer to prevent duplicate work. Must be called before submitting corrections.

```
POST /v1/faxes/{fax_job_id}/review/claim
Authorization: Bearer <token>
```

No request body needed — the reviewer identity is taken from the JWT token.

Response `200 OK`:
```json
{
  "fax_job_id": "550e8400-...",
  "claimed_by": "reviewer@clinic.com",
  "claimed_at": "2025-08-01T11:00:00.000Z"
}
```

Response `409 Conflict` — job already claimed by another reviewer:
```json
{
  "detail": "Job already claimed by reviewer@otherclinic.com"
}
```

---

### Submit Review

Submit the review outcome and any field corrections. The job status changes to `COMPLETED` after submission.

```
POST /v1/faxes/{fax_job_id}/review/submit
Authorization: Bearer <token>
Content-Type: application/json
```

Request body:
```json
{
  "outcome": "APPROVED",
  "notes": "Member ID was OCR'd incorrectly — corrected from MBR001234 to MBR001234567",
  "corrected_fields": [
    {
      "field_key": "member_id",
      "corrected_value": "MBR001234567"
    },
    {
      "field_key": "service_code",
      "corrected_value": "H2012"
    }
  ]
}
```

**Request schema:**

| Field | Type | Required | Description |
|---|---|---|---|
| `outcome` | string | Yes | `APPROVED`, `REJECTED`, `ESCALATED` |
| `notes` | string | No | Free-text reviewer notes |
| `corrected_fields` | array | No | List of field corrections |
| `corrected_fields[].field_key` | string | Yes | The field to correct |
| `corrected_fields[].corrected_value` | string | Yes | The correct value (empty string allowed for false-positive fields) |

Response `200 OK`:
```json
{
  "fax_job_id": "550e8400-...",
  "status": "COMPLETED",
  "corrections_applied": 2,
  "reviewed_by": "reviewer@clinic.com",
  "reviewed_at": "2025-08-01T11:15:00.000Z"
}
```

After submission:
- Corrected fields are stored with `confidence = 1.0` and `source = HUMAN_REVIEW`
- Original machine-extracted values are archived in the candidates list
- Corrections are recorded as training examples for future model improvement
- All actions are written to the HIPAA audit log

---

## Template Admin — Port 8002

Manage the payer form templates used for field extraction. Templates teach the system where fields appear on specific payer forms.

### List Templates

```
GET /v1/templates
Authorization: Bearer <token>
```

Query parameters:
- `payer_filter` — filter by payer name
- `active_only` — boolean, default `false`

Response `200 OK`:
```json
{
  "total": 8,
  "items": [
    {
      "template_id": "a1b2c3d4-...",
      "payer_name": "ANTHEM",
      "doc_type": "PRIOR_AUTH_FORM",
      "template_name": "Anthem PA Form v2",
      "is_active": true,
      "version_count": 2,
      "created_at": "2025-07-01T00:00:00.000Z"
    }
  ]
}
```

---

### Create Template

```
POST /v1/templates
Authorization: Bearer <token>
Content-Type: application/json
```

Request body:
```json
{
  "payer_name": "ANTHEM",
  "doc_type": "PRIOR_AUTH_FORM",
  "template_name": "Anthem PA Form v2",
  "description": "Standard Anthem prior authorization form, revised 2025"
}
```

**Valid `payer_name` values:** `ANTHEM`, `CARESOURCE`, `MOLINA`, `BUCKEYE`, `HUMANA`, `UNITED_HEALTH`, `AMERIHEALTH`, `AETNA`, `PARAMOUNT`, `PROMEDICA`, `UNKNOWN`

**Valid `doc_type` values:** `PRIOR_AUTH_FORM`, `PRIOR_AUTH_APPROVAL`, `PRIOR_AUTH_DENIAL`, `PEER_TO_PEER_DENIAL`, `FAX_COVER_SHEET`, `HIPAA_RELEASE`, `CLINICAL_NOTES`, `LAB_RESULTS`, `OTHER`

Response `201 Created`:
```json
{
  "template_id": "a1b2c3d4-...",
  "payer_name": "ANTHEM",
  "doc_type": "PRIOR_AUTH_FORM",
  "template_name": "Anthem PA Form v2",
  "is_active": true,
  "created_at": "2025-08-01T00:00:00.000Z"
}
```

---

### Get Template

```
GET /v1/templates/{template_id}
Authorization: Bearer <token>
```

Response `200 OK`:
```json
{
  "template_id": "a1b2c3d4-...",
  "payer_name": "ANTHEM",
  "doc_type": "PRIOR_AUTH_FORM",
  "template_name": "Anthem PA Form v2",
  "description": "Standard Anthem prior authorization form",
  "is_active": true,
  "versions": [
    {
      "version_id": "v1x2y3z4-...",
      "version_label": "v1.0",
      "is_active": true,
      "match_min_score": 0.75,
      "match_phash_threshold": 10,
      "field_count": 9,
      "created_at": "2025-07-01T00:00:00.000Z"
    }
  ]
}
```

---

### Create Template Version

```
POST /v1/templates/{template_id}/versions
Authorization: Bearer <token>
Content-Type: application/json
```

Request body:
```json
{
  "version_label": "v2.0",
  "match_min_score": 0.75,
  "match_phash_threshold": 10,
  "match_orb_min_matches": 20,
  "notes": "Updated for new form layout released Q3 2025"
}
```

Response `201 Created`:
```json
{
  "version_id": "v1x2y3z4-...",
  "template_id": "a1b2c3d4-...",
  "version_label": "v2.0",
  "is_active": true,
  "created_at": "2025-08-01T00:00:00.000Z"
}
```

---

### Upload Template Sample Image

Upload a sample page from the payer form. The system extracts a perceptual hash (pHash) from this image, which is used to identify matching documents in the pipeline.

```
POST /v1/templates/versions/{version_id}/samples
Authorization: Bearer <token>
Content-Type: multipart/form-data
```

Form fields:
- `file` — PNG or JPEG image of the form page (300 DPI recommended)
- `page_number` — which page of the form this image represents

Response `201 Created`:
```json
{
  "sample_id": "s1a2b3c4-...",
  "version_id": "v1x2y3z4-...",
  "page_number": 1,
  "phash": 4611686018427387904,
  "image_width": 2550,
  "image_height": 3300,
  "created_at": "2025-08-01T00:00:00.000Z"
}
```

---

### Add Field Definition

Define where a specific field appears on the template. Fields use normalized coordinates (0.0 to 1.0) relative to the page size.

```
POST /v1/templates/versions/{version_id}/fields
Authorization: Bearer <token>
Content-Type: application/json
```

Request body:
```json
{
  "field_key": "member_id",
  "field_label": "Member ID",
  "page_number": 1,
  "roi_x0": 0.10,
  "roi_y0": 0.20,
  "roi_x1": 0.45,
  "roi_y1": 0.25,
  "is_required": true,
  "field_type": "text",
  "validation_regex": "^[A-Z0-9]{8,15}$",
  "label_search_text": "Member ID",
  "label_search_region": {
    "x0": 0.05, "y0": 0.18, "x1": 0.45, "y1": 0.27
  }
}
```

**Field definition schema:**

| Field | Type | Required | Description |
|---|---|---|---|
| `field_key` | string | Yes | Internal field identifier (must match extractable field keys) |
| `field_label` | string | No | Human-readable label |
| `page_number` | integer | Yes | Which page of the form (1-indexed) |
| `roi_x0/y0/x1/y1` | float 0–1 | Yes | Region of interest bounding box |
| `is_required` | boolean | No | Whether this field is expected on every document |
| `field_type` | string | No | `text`, `date`, `number`, `phone`, `npi`, `code` |
| `validation_regex` | string | No | Regex pattern for value validation |
| `label_search_text` | string | No | OCR text to search for (label-anchored extraction) |
| `label_search_region` | object | No | Constrained region to search for the label |

Response `201 Created`:
```json
{
  "field_id": "f1g2h3i4-...",
  "version_id": "v1x2y3z4-...",
  "field_key": "member_id",
  "page_number": 1,
  "roi_x0": 0.10,
  "roi_y0": 0.20,
  "roi_x1": 0.45,
  "roi_y1": 0.25,
  "is_required": true
}
```

---

### List Field Definitions

```
GET /v1/templates/versions/{version_id}/fields
Authorization: Bearer <token>
```

Response `200 OK`:
```json
{
  "total": 9,
  "fields": [
    {
      "field_id": "f1g2h3i4-...",
      "field_key": "member_id",
      "field_label": "Member ID",
      "page_number": 1,
      "roi_x0": 0.10,
      "roi_y0": 0.20,
      "roi_x1": 0.45,
      "roi_y1": 0.25,
      "is_required": true,
      "field_type": "text"
    }
  ]
}
```

---

### Update Field Definition

```
PUT /v1/templates/versions/{version_id}/fields/{field_id}
Authorization: Bearer <token>
Content-Type: application/json
```

Request body: same structure as create, all fields optional.

Response `200 OK`: updated field object.

---

### Delete Field Definition

```
DELETE /v1/templates/versions/{version_id}/fields/{field_id}
Authorization: Bearer <token>
```

Response `204 No Content`.

---

### Test Template Match

Upload a document image to test which template it matches and at what score.

```
POST /v1/templates/test-match
Authorization: Bearer <token>
Content-Type: multipart/form-data
```

Form fields:
- `file` — PNG or JPEG page image
- `payer_hint` — optional payer name to restrict search

Response `200 OK`:
```json
{
  "matched": true,
  "template_id": "a1b2c3d4-...",
  "template_name": "Anthem PA Form v2",
  "payer_name": "ANTHEM",
  "match_score": 0.89,
  "phash_distance": 3,
  "anchor_score": 0.94
}
```

---

## Analytics — Port 8002

All analytics endpoints return aggregated data across all tenants (admin) or scoped to the requesting user's tenant.

### Overall Summary

```
GET /v1/analytics/summary
Authorization: Bearer <token>
```

Response `200 OK`:
```json
{
  "period_days": 30,
  "total_jobs": 1248,
  "completed": 1190,
  "needs_review": 42,
  "failed": 16,
  "avg_confidence": 0.879,
  "payer_accuracy_pct": 100.0,
  "avg_processing_time_sec": 38.4
}
```

---

### Per-Payer Breakdown

```
GET /v1/analytics/payer-breakdown
Authorization: Bearer <token>
```

Response `200 OK`:
```json
{
  "payers": [
    {
      "payer": "ANTHEM",
      "total_jobs": 312,
      "completed": 301,
      "avg_confidence": 0.903,
      "review_rate_pct": 3.5,
      "avg_fields_found": 12.8
    }
  ]
}
```

---

### Field Accuracy

```
GET /v1/analytics/field-accuracy
Authorization: Bearer <token>
```

Response `200 OK`:
```json
{
  "fields": [
    {
      "field_key": "member_id",
      "extraction_rate_pct": 98.2,
      "avg_confidence": 0.921,
      "human_correction_rate_pct": 1.4
    }
  ]
}
```

---

### Mismatch Metrics

Returns fields where the extraction sources disagreed, which may indicate a template configuration issue or a new form variation.

```
GET /v1/analytics/mismatches
Authorization: Bearer <token>
```

Query parameters:
- `hours` — lookback window, default `24`
- `threshold` — mismatch rate above which fields are included, default `0.30`

Response `200 OK`:
```json
{
  "period_hours": 24,
  "threshold": 0.30,
  "mismatches": [
    {
      "field_key": "service_code",
      "mismatch_rate": 0.38,
      "sample_count": 21,
      "alert": true
    }
  ]
}
```

---

### Hourly Processing Trend

```
GET /v1/analytics/hourly-trend
Authorization: Bearer <token>
```

Query parameters:
- `hours` — number of hours to include, default `48`

Response `200 OK`:
```json
{
  "hours": 48,
  "data": [
    {
      "hour": "2025-08-01T10:00:00.000Z",
      "jobs_received": 12,
      "jobs_completed": 11,
      "jobs_failed": 0,
      "avg_confidence": 0.892
    }
  ]
}
```

---

## Model Versions — Port 8002

Track and promote LayoutLM adapter versions. Each time the model is fine-tuned, a new version is registered here.

### List Versions

```
GET /v1/models
Authorization: Bearer <token>
```

Response `200 OK`:
```json
{
  "total": 3,
  "versions": [
    {
      "version_id": "m1n2o3p4-...",
      "model_name": "layoutlm-document-qa",
      "adapter_path": "/models/layoutlm-v3/adapter",
      "is_active": true,
      "promoted_at": "2025-08-01T00:00:00.000Z",
      "accuracy_overall": 0.961,
      "created_at": "2025-07-28T00:00:00.000Z"
    }
  ]
}
```

---

### Get Active Version

```
GET /v1/models/active
Authorization: Bearer <token>
```

Response `200 OK`: same structure as a single version object above.

---

### Register New Version

Called automatically by the fine-tuning task. Can also be called manually to register an externally trained adapter.

```
POST /v1/models
Authorization: Bearer <token>
Content-Type: application/json
```

Request body:
```json
{
  "model_name": "layoutlm-document-qa",
  "adapter_path": "/models/layoutlm-v4/adapter",
  "description": "Fine-tuned on 87 human-reviewed corrections",
  "training_examples": 87
}
```

Response `201 Created`: version object.

---

### Promote Version

Make a registered version the active production version. The worker picks up the new adapter on its next job.

```
PUT /v1/models/{version_id}/promote
Authorization: Bearer <token>
```

Response `200 OK`:
```json
{
  "version_id": "m1n2o3p4-...",
  "is_active": true,
  "promoted_at": "2025-08-01T12:00:00.000Z"
}
```

---

### Record Metrics

```
POST /v1/models/{version_id}/metrics
Authorization: Bearer <token>
Content-Type: application/json
```

Request body:
```json
{
  "accuracy_overall": 0.961,
  "accuracy_by_field": {
    "member_id": 0.978,
    "service_code": 0.944,
    "prior_auth_number": 0.956
  },
  "eval_set_size": 120
}
```

Response `200 OK`: updated version object.

---

## Query API — Port 8003

High-performance read-only API for integration with downstream systems. Returns the same extraction data as the Ingress API results endpoint, but is optimized for high-frequency polling.

### Get Results

```
GET /v1/query/{fax_job_id}
Authorization: Bearer <token>
```

Response: same structure as `GET /v1/faxes/{fax_job_id}/results` from the Ingress API.

### Search Jobs

```
GET /v1/query/search
Authorization: Bearer <token>
```

Query parameters:
- `member_id` — search by extracted member ID value
- `patient_name` — partial name match
- `payer` — filter by payer
- `date_from` / `date_to` — filter by creation date (ISO 8601)
- `limit` / `offset` — pagination

Response `200 OK`: same structure as the Ingress API list endpoint.

---

## Common Error Responses

| HTTP Code | Meaning |
|---|---|
| 400 Bad Request | Invalid request body or parameters |
| 401 Unauthorized | Missing or expired Bearer token |
| 403 Forbidden | Authenticated but not authorized (wrong tenant) |
| 404 Not Found | Resource does not exist |
| 409 Conflict | State conflict (e.g., job already claimed) |
| 413 Payload Too Large | File exceeds `API_MAX_UPLOAD_SIZE_MB` |
| 422 Unprocessable Entity | Request body validation failed (field-level errors included) |
| 429 Too Many Requests | Rate limit exceeded |
| 500 Internal Server Error | Unexpected server-side error |
| 503 Service Unavailable | Downstream dependency unavailable (queue, database) |

All error responses follow this structure:
```json
{
  "detail": "Human-readable error message"
}
```

422 responses include per-field validation details:
```json
{
  "detail": [
    {
      "loc": ["body", "payer_name"],
      "msg": "value is not a valid enumeration member",
      "type": "value_error.enum"
    }
  ]
}
```

---

## Rate Limiting

All endpoints enforce a token-bucket rate limit per authenticated user:

- Default: 60 requests per minute
- Upload endpoint: 10 requests per minute

When the limit is exceeded, the response includes:
```
HTTP 429 Too Many Requests
Retry-After: 15
```

The `Retry-After` header indicates how many seconds to wait before retrying.
