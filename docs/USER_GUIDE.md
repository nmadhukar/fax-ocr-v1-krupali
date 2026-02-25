# End-User Guide — Healthcare Fax OCR Processing System

This guide is for **operational staff, system administrators, and reviewers** who use the system day-to-day. It covers how to upload faxes, monitor processing, review flagged documents, manage templates, and interpret analytics.

---

## Table of Contents

1. [System Overview for Users](#1-system-overview-for-users)
2. [Getting Started](#2-getting-started)
3. [Operations Console UI](#3-operations-console-ui)
4. [Uploading Faxes](#4-uploading-faxes)
5. [Checking Processing Status](#5-checking-processing-status)
6. [Retrieving Extraction Results](#6-retrieving-extraction-results)
7. [Understanding Extraction Output](#7-understanding-extraction-output)
8. [Review Workflow — Step by Step](#8-review-workflow--step-by-step)
9. [Template Administration](#9-template-administration)
10. [Analytics Dashboard](#10-analytics-dashboard)
11. [Model Version Management](#11-model-version-management)
12. [Querying Historical Data](#12-querying-historical-data)
13. [Supported Payers](#13-supported-payers)
14. [Supported Document Types](#14-supported-document-types)
15. [Extracted Fields Reference](#15-extracted-fields-reference)
16. [Understanding Confidence Scores](#16-understanding-confidence-scores)
17. [Common Workflows](#17-common-workflows)
18. [Troubleshooting](#18-troubleshooting)
19. [HIPAA & Security Notes](#19-hipaa--security-notes)

---

## 1. System Overview for Users

The Healthcare Fax OCR Processing System automatically:

1. **Receives** fax documents (PDF, TIFF, or image files)
2. **Identifies** the insurance payer and document type
3. **Extracts** key data fields (member ID, auth numbers, dates, etc.)
4. **Validates** the extracted data for consistency
5. **Routes** uncertain results to human reviewers
6. **Learns** from reviewer corrections to improve over time

### What the System Extracts

The system extracts up to 15 fields from each fax document:

| Field | Description | Example |
|-------|-------------|---------|
| `member_id` | Insurance member identifier | MBR001234 |
| `prior_auth_number` | Prior authorization reference | PA-00123 |
| `patient_name` | Patient full name | Jane Doe |
| `patient_dob` | Patient date of birth | 01/15/1985 |
| `auth_effective_date` | Authorization start date | 01/15/2025 |
| `auth_expiration_date` | Authorization end date | 03/15/2025 |
| `next_review_date` | Next review date | 02/15/2025 |
| `provider_name` | Provider/physician name | Dr. John Smith |
| `provider_npi` | National Provider Identifier | 1234567890 |
| `provider_phone` | Provider phone number | (614) 555-1234 |
| `provider_fax` | Provider fax number | (614) 555-5678 |
| `service_code` | CPT/HCPCS procedure code | 99213 |
| `diagnosis_code` | ICD-10 diagnosis code | F32.1 |
| `units_requested` | Number of units authorized | 10 |
| `decision` | Authorization decision | APPROVED / DENIED |

---

## 2. Getting Started

### Accessing the System

The system exposes three API endpoints:

| Service | URL | Purpose |
|---------|-----|---------|
| **Upload & Results** | `http://your-server:8001` | Upload faxes, check status, get results |
| **Review & Admin** | `http://your-server:8002` | Review workflow, template management |
| **Query** | `http://your-server:8003` | Search historical fax data |

### Authentication

All API calls require a JWT Bearer token:
```
Authorization: Bearer your-jwt-token-here
```

Contact your system administrator to obtain credentials.

### Interactive API Documentation

When debug mode is enabled, Swagger UI is available at:
- `http://your-server:8001/docs` — Upload API
- `http://your-server:8002/docs` — Review API
- `http://your-server:8003/docs` — Query API

---

## 3. Operations Console UI

The Operations Console is a browser-based interface that consolidates all system workflows into a single professional UI. It is served directly by the Review API at **`http://your-server:8002/ui`**.

### Accessing the Console

Open your browser and navigate to:
```
http://localhost:8002/ui
```

The console requires no separate installation — it is bundled with the Review API and served as static files. The root URL (`http://localhost:8002/`) automatically redirects to `/ui/`.

### Console Layout

The interface has two main areas:

| Area | Description |
|------|-------------|
| **Connection Profile** (left sidebar) | Configure API endpoints, JWT token, reviewer ID. Saved in browser `localStorage`. |
| **Workspace** (main area) | Five tabs covering all system workflows. |

### The Five Tabs

#### Dashboard Tab

A visual analytics overview with real-time charts and KPI cards. Click **Refresh Dashboard** to load data for a configurable lookback period (default 30 days). The dashboard displays:

- **KPI Cards** — Total jobs, completed, failed, needs-review counts, auto-finalize rate, average confidence, and average processing time
- **Processing Volume** — Bar chart showing daily processing volume over the lookback period
- **Document Type Distribution** — Doughnut chart of document types (Prior Auth Form, Approval, Denial, etc.)
- **Payer Performance** — Bar chart comparing auto-finalize rates across payers
- **Confidence Distribution** — Bar chart showing confidence score distribution across all jobs

#### Workflow Tab

The primary operational workspace with five sections:

| Section | Purpose |
|---------|---------|
| **Upload Fax** | Upload PDF/TIFF/PNG/JPEG documents with tenant ID and optional payer hint. Accepted file types: `.pdf`, `.tiff`, `.tif`, `.png`, `.jpg`, `.jpeg`. |
| **Fax Jobs** | Browse and filter all fax jobs by status, tenant, with pagination. Click any job to select it for inspection or review. |
| **Job Inspector** | Fetch the status, extraction results, or raw OCR data for a specific job by ID. |
| **Review Queue (HITL)** | Load unclaimed or pending reviews. Claim reviews, release expired claims. Each queue item shows job ID, priority, claimed-by status, and review reasons. |
| **Review Packet Workspace** | The full human-in-the-loop review interface. Displays page images with presigned URLs, extracted fields with confidence scores, flagged fields, and inline correction inputs. Submit corrections directly from this screen. |

#### Templates Tab

Complete template lifecycle management:

| Section | Purpose |
|---------|---------|
| **Template Catalog** | List templates filtered by payer name and active status. |
| **Create or Modify Template** | Create new templates (payer, doc type, name, description) or update/delete existing ones by ID. |
| **Version Management** | Create template versions with matching thresholds (min score, pHash threshold, ORB min matches). Activate versions or update thresholds. |
| **Samples and Fields** | Upload sample page images for a version. Define field ROI coordinates (x0, y0, x1, y1 in 0.0–1.0 range), target page, validation regex, and required flag. List all fields for a version. |
| **Template Testing** | Test template matching against an uploaded image. Test field extraction for a specific version. Use the Suggest ROI tool to auto-detect field bounding boxes from a processed fax. |

#### Intelligence Tab

Data querying, analytics, and ML model management:

| Section | Purpose |
|---------|---------|
| **Search Extracted Data** | Run Tier 1 (structured SQL), Tier 2 (semantic vector), or Tier 3 (summarizer) queries against historical extraction results. Results display in a table with fax job ID, field key, value, confidence, and similarity score. |
| **Analytics Dashboard** | Run quality overview, all-payer comparison, single-payer analysis, feedback summary, or threshold recalibration reports. Configurable time window (days). |
| **Model Version Management** | List model versions, view the active version for a model type, register new versions, promote a version to active, update metrics JSON, or delete versions. |

#### API Console Tab

A raw API explorer for advanced use:

- Select a target service (Ingress, Review, Query, or Custom URL)
- Choose HTTP method (GET, POST, PUT, PATCH, DELETE)
- Specify the path, query string, and optional JSON body
- View the full response including HTTP status, URL, and response data

This is useful for endpoints not explicitly wired into the curated tab screens.

### Connection Profile

The sidebar contains five configuration fields:

| Field | Default | Purpose |
|-------|---------|---------|
| **Ingress API Base URL** | `http://localhost:8001` | Upload and status API endpoint |
| **Review API Base URL** | `http://localhost:8002` | Review, templates, analytics API endpoint |
| **Query API Base URL** | `http://localhost:8003` | Query and search API endpoint |
| **Bearer Token** | (empty) | JWT token for staging/production authentication |
| **Reviewer ID** | (empty) | Your reviewer identifier for claiming and submitting reviews |

Click **Save Profile** to persist settings to browser `localStorage`. Click **Test Health** to verify connectivity to all three API services. Click **Reset Defaults** to restore factory settings.

### Activity Feed

The sidebar displays a live activity feed showing the last 16 actions with timestamps. Activity items are color-coded:
- **Info** (teal left border) — Normal operations
- **Error** (red left border) — Failed API calls or validation errors

### Toast Notifications

Floating notifications appear in the bottom-right corner:
- **Success** (green) — Operation completed successfully (auto-dismiss after 3.6 seconds)
- **Error** (red) — Operation failed (auto-dismiss after 7 seconds)
- **Info** (teal) — Informational messages (auto-dismiss after 3.6 seconds)

### Keyboard and Accessibility

The console is built with WCAG 2.1 AA compliance:
- Full keyboard navigation with visible focus indicators on all interactive elements
- WAI-ARIA tab pattern (`role="tablist"`, `role="tab"`, `role="tabpanel"`, `aria-selected`)
- Live regions (`aria-live="polite"`) on the activity feed and toast notifications
- Lightbox image viewer with `role="dialog"` and `aria-modal="true"`
- Dynamic form inputs include descriptive `aria-label` attributes
- `prefers-reduced-motion` media query disables animations for users who prefer reduced motion

---

## 4. Uploading Faxes

### Using cURL

```bash
curl -X POST http://your-server:8001/v1/faxes/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/fax.pdf" \
  -F "tenant_id=your_tenant" \
  -F "payer_hint=ANTHEM"
```

### Accepted File Types

| Format | MIME Type | Max Size |
|--------|-----------|----------|
| PDF | application/pdf | 50 MB |
| TIFF | image/tiff | 50 MB |
| PNG | image/png | 50 MB |
| JPEG | image/jpeg | 50 MB |

### Payer Hint

Providing a `payer_hint` helps the system match the correct template faster. Valid values:

`ANTHEM`, `UNITED_HEALTH`, `HUMANA`, `MOLINA`, `CARESOURCE`, `BUCKEYE`, `AMERIHEALTH`, `AETNA`, `PARAMOUNT`, `PROMEDICA`

If omitted, the system auto-detects the payer from the document content.

### Duplicate Detection

If you upload the same file twice, the system returns the existing job instead of creating a duplicate. This is based on SHA-256 file hashing.

---

## 5. Checking Processing Status

```bash
curl http://your-server:8001/v1/faxes/{fax_job_id} \
  -H "Authorization: Bearer $TOKEN"
```

### Status Values

| Status | Meaning | Action Required |
|--------|---------|-----------------|
| `PENDING` | Uploaded, waiting in queue | None — wait for processing |
| `PROCESSING` | Currently being processed | None — typically takes 20-60 seconds |
| `COMPLETED` | Successfully processed | Results available |
| `NEEDS_REVIEW` | Processed but needs human review | Review and correct flagged fields |
| `FAILED` | Processing failed | Check error details, re-upload if needed |

---

## 6. Retrieving Extraction Results

```bash
curl http://your-server:8001/v1/faxes/{fax_job_id}/results \
  -H "Authorization: Bearer $TOKEN"
```

Results are only available when the job status is `COMPLETED` or `NEEDS_REVIEW`.

---

## 7. Understanding Extraction Output

Each field in the results has four properties:

```json
{
  "value": "MBR001234",
  "confidence": 0.95,
  "source": "TEMPLATE_OCR",
  "not_present": false
}
```

| Property | Meaning |
|----------|---------|
| `value` | The extracted text (null if not found) |
| `confidence` | How confident the system is (0.0 = no confidence, 1.0 = certain) |
| `source` | Which extraction method produced the value |
| `not_present` | True if the field doesn't exist in this document |

### Source Methods

| Source | Description | Typical Confidence |
|--------|-------------|-------------------|
| `TEMPLATE_OCR` | Extracted from a known template region | 0.85–0.99 |
| `LAYOUTLM` | Extracted by AI document understanding model | 0.60–0.90 |
| `HYBRID` | Multiple methods agreed on the same value | 0.90–1.00 |
| `HUMAN_REVIEW` | Corrected by a human reviewer | 1.00 |
| `VLM` | Vision-language model extraction | 0.50–0.85 |

### Summary Section

The results include a summary:

```json
{
  "total_fields": 15,
  "fields_found": 12,
  "fields_not_present": 3,
  "fields_flagged_for_review": 2,
  "flagged_field_keys": ["member_id", "auth_effective_date"]
}
```

---

## 8. Review Workflow — Step by Step

When the system isn't confident about some fields, the job is routed for human review.

### Step 1: Find Jobs Needing Review

```bash
# List unclaimed reviews
curl http://your-server:8002/v1/faxes/reviews/unclaimed \
  -H "Authorization: Bearer $TOKEN"
```

### Step 2: Claim a Review

```bash
curl -X POST http://your-server:8002/v1/faxes/{fax_job_id}/review/claim \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Once claimed, you have 30 minutes to submit corrections before the claim expires.

### Step 3: View the Review Packet

```bash
curl http://your-server:8002/v1/faxes/{fax_job_id}/review-packet \
  -H "Authorization: Bearer $TOKEN"
```

The review packet includes:
- **Page images** with presigned URLs (viewable in browser for 1 hour)
- **Extracted fields** with all candidates from different extraction methods
- **Flagged fields** highlighted with reasons (LOW_CONFIDENCE, MISSING_VALUE)
- **Template match** information

### Step 4: Submit Corrections

```bash
curl -X POST http://your-server:8002/v1/faxes/{fax_job_id}/review/submit \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "corrected_fields": [
      {"field_key": "member_id", "corrected_value": "MBR001234"},
      {"field_key": "auth_effective_date", "corrected_value": "2025-01-15"}
    ]
  }'
```

### What Happens After Submission

1. Your corrections are applied to the extraction results with 100% confidence
2. The job status changes to `COMPLETED`
3. Correction feedback is saved for system improvement
4. Training labels are created for AI model fine-tuning
5. Future documents similar to this one will be extracted more accurately

### Tips for Reviewers

- Focus on **flagged fields** first — they're highlighted because the system is uncertain
- Look at the **candidates** list — sometimes the correct value is a lower-ranked candidate
- Use the **page images** to verify values against the original document
- If a field truly doesn't exist in the document, you don't need to correct it

---

## 9. Template Administration

Templates define where the system should look for each field on a document. Admin access required.

### When to Create a New Template

- A new payer starts sending faxes
- An existing payer changes their form layout
- You want to improve extraction for a specific document type

### Template Creation Workflow

1. **Create template** — Associate with a payer and document type
2. **Create version** — Set matching thresholds
3. **Upload sample** — A representative page image from the payer's form
4. **Define fields** — Mark where each field appears on the page
5. **Test match** — Verify the template matches actual documents
6. **Test extract** — Verify field extraction works
7. **Activate** — Make the version live for production

### Field ROI Definition Tips

- ROI coordinates range from 0.0 (top/left) to 1.0 (bottom/right)
- Make the bounding box slightly larger than the field to account for alignment variations
- Use the `suggest-roi` endpoint to get automatic recommendations
- Test with multiple document samples before activating

---

## 10. Analytics Dashboard

Admin users can access quality analytics to monitor system performance.

### Quality Overview

```bash
curl "http://your-server:8002/v1/analytics/quality?days=30" \
  -H "Authorization: Bearer $TOKEN"
```

Key metrics:
- **Auto-finalize rate** — Percentage of faxes processed without human review
- **Average confidence** — Mean extraction confidence across all jobs
- **Processing time** — Average, P50, P95 pipeline execution time

### Per-Payer Analysis

```bash
curl "http://your-server:8002/v1/analytics/payer/ANTHEM?days=30" \
  -H "Authorization: Bearer $TOKEN"
```

### Feedback Analysis

```bash
curl "http://your-server:8002/v1/analytics/feedback-summary?days=30" \
  -H "Authorization: Bearer $TOKEN"
```

Shows which fields are most frequently corrected by reviewers — useful for identifying template improvements needed.

---

## 11. Model Version Management

The system uses LayoutLM (an AI model) for document understanding. Admins can manage model versions.

### Viewing Current Model

```bash
curl "http://your-server:8002/v1/models/active/LAYOUTLM" \
  -H "Authorization: Bearer $TOKEN"
```

### After Fine-Tuning

When the system fine-tunes a new model version:
1. View its metrics to ensure it's better than the current model
2. Promote it if accuracy improved
3. The system automatically uses the promoted version for new jobs

---

## 12. Querying Historical Data

The Query API lets you search through historical extraction results.

### Structured Search (Tier 1)

Search by field name or value:

```bash
curl -X POST http://your-server:8003/v1/query \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "MBR001234", "tier": 1, "limit": 10}'
```

### Semantic Search (Tier 2)

Natural language search over document content:

```bash
curl -X POST http://your-server:8003/v1/query \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "prior authorization for physical therapy", "tier": 2, "limit": 10}'
```

---

## 13. Supported Payers

| Payer | Enum Value | Member ID Format |
|-------|-----------|------------------|
| Anthem Blue Cross Blue Shield | `ANTHEM` | 8-14 alphanumeric |
| UnitedHealthcare | `UNITED_HEALTH` | 9-12 alphanumeric |
| Humana | `HUMANA` | 8-14 alphanumeric |
| Molina Healthcare | `MOLINA` | 10-12 digits |
| CareSource | `CARESOURCE` | 10-14 alphanumeric |
| Buckeye Health Plan | `BUCKEYE` | 10 digits |
| AmeriHealth Caritas | `AMERIHEALTH` | 6-12 alphanumeric (optional suffix) |
| Aetna | `AETNA` | 8-14 alphanumeric |
| Paramount Advantage | `PARAMOUNT` | 8-14 alphanumeric |
| ProMedica | `PROMEDICA` | 8-12 digits |

---

## 14. Supported Document Types

| Document Type | Enum Value | Description |
|--------------|-----------|-------------|
| Prior Auth Form | `PRIOR_AUTH_FORM` | Request for prior authorization |
| Prior Auth Approval | `PRIOR_AUTH_APPROVAL` | Approved authorization |
| Prior Auth Denial | `PRIOR_AUTH_DENIAL` | Denied authorization |
| Peer-to-Peer Denial | `PEER_TO_PEER_DENIAL` | Denial requiring peer review |
| Fax Cover Sheet | `FAX_COVER_SHEET` | Cover page (auto-detected and skipped) |
| HIPAA Release | `HIPAA_RELEASE` | HIPAA authorization form |
| Clinical Notes | `CLINICAL_NOTES` | Clinical documentation |
| Lab Results | `LAB_RESULTS` | Laboratory test results |
| Other | `OTHER` | Unclassified document |

---

## 15. Extracted Fields Reference

### Critical Fields

These fields are held to higher confidence thresholds and are always flagged if missing or low-confidence:

| Field | Confidence Threshold | Format |
|-------|---------------------|--------|
| `member_id` | 0.85–0.90 (payer-specific) | Alphanumeric, payer-specific regex |
| `prior_auth_number` | 0.85 | Alphanumeric, 6-15 characters |
| `auth_effective_date` | 0.80 | Date (MM/DD/YYYY or YYYY-MM-DD) |
| `auth_expiration_date` | 0.80 | Date (must be after effective date) |
| `decision` | 0.85 | APPROVED, DENIED, or PENDING |

### Non-Critical Fields

| Field | Confidence Threshold | Format |
|-------|---------------------|--------|
| `patient_name` | 0.75 | Free text |
| `patient_dob` | 0.75 | Date (must be in the past) |
| `provider_name` | 0.75 | Free text |
| `provider_npi` | 0.75 | 10 digits (Luhn check) |
| `provider_phone` | 0.75 | 10 digits |
| `provider_fax` | 0.75 | 10 digits |
| `service_code` | 0.75 | 5 digits (CPT) or letter+4 digits (HCPCS) |
| `diagnosis_code` | 0.75 | ICD-10: letter+2 digits[.extension] |
| `units_requested` | 0.75 | Number |
| `next_review_date` | 0.75 | Date |

---

## 16. Understanding Confidence Scores

### What Confidence Means

| Range | Meaning | Typical Outcome |
|-------|---------|-----------------|
| 0.95–1.00 | Very high — multiple methods agree | Auto-finalized |
| 0.85–0.95 | High — template extraction succeeded | Auto-finalized |
| 0.70–0.85 | Moderate — some uncertainty | May be flagged for review |
| 0.50–0.70 | Low — model is uncertain | Flagged for review |
| 0.00–0.50 | Very low — likely incorrect | Flagged for review |
| 0.00 | Not found | Marked as `not_present` |

### Overall Confidence

The job's `overall_confidence` is a weighted average of all field confidences:
- Critical fields count 2x
- Non-critical fields count 1x
- If overall confidence >= 0.90 → auto-finalized as `COMPLETED`
- If overall confidence < 0.90 → routed to `NEEDS_REVIEW`

---

## 17. Common Workflows

### Workflow 1: Bulk Upload from Fax Gateway

```bash
#!/bin/bash
for file in /incoming/faxes/*.pdf; do
  curl -X POST http://your-server:8001/v1/faxes/upload \
    -H "Authorization: Bearer $TOKEN" \
    -F "file=@$file" \
    -F "tenant_id=our_clinic"
  echo "Uploaded: $file"
done
```

### Workflow 2: Poll for Results

```bash
#!/bin/bash
JOB_ID="your-job-id"
while true; do
  STATUS=$(curl -s http://your-server:8001/v1/faxes/$JOB_ID \
    -H "Authorization: Bearer $TOKEN" | python -c "import sys,json; print(json.load(sys.stdin)['status'])")

  if [ "$STATUS" = "COMPLETED" ] || [ "$STATUS" = "NEEDS_REVIEW" ]; then
    echo "Job $JOB_ID finished with status: $STATUS"
    curl http://your-server:8001/v1/faxes/$JOB_ID/results \
      -H "Authorization: Bearer $TOKEN"
    break
  fi
  echo "Status: $STATUS — waiting..."
  sleep 5
done
```

### Workflow 3: Daily Review Queue

```bash
# Get all unclaimed reviews
curl "http://your-server:8002/v1/faxes/reviews/unclaimed?limit=50" \
  -H "Authorization: Bearer $TOKEN"
```

### Workflow 4: EMR Integration

```python
import requests

def get_fax_results(fax_job_id: str, token: str) -> dict:
    """Retrieve extraction results for EMR system integration."""
    resp = requests.get(
        f"http://your-server:8001/v1/faxes/{fax_job_id}/results",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    data = resp.json()

    # Extract key fields for EMR
    fields = data["fields"]
    return {
        "member_id": fields.get("member_id", {}).get("value"),
        "auth_number": fields.get("prior_auth_number", {}).get("value"),
        "decision": fields.get("decision", {}).get("value"),
        "effective_date": fields.get("auth_effective_date", {}).get("value"),
        "expiration_date": fields.get("auth_expiration_date", {}).get("value"),
        "confidence": data.get("overall_confidence"),
        "needs_review": data.get("needs_review"),
    }
```

---

## 18. Troubleshooting

### "Job stuck in PENDING"

- The processing queue may be full or the worker is down
- Check worker health: `curl http://your-server:8001/health`
- Re-upload the file — duplicate detection will re-queue the existing job

### "Job stuck in PROCESSING"

- Processing typically takes 20-60 seconds
- For very long documents (50+ pages), processing can take several minutes
- If stuck for >5 minutes, the job may have encountered an error

### "Results show 'not_present' for a field I can see"

- The template's ROI may not cover the field location on this specific form variant
- The OCR engine may not have detected the text (low image quality)
- Contact admin to adjust the template field definitions

### "Low confidence on a clearly readable field"

- The field may not match the expected format (regex validation)
- The template ROI may be slightly misaligned
- Multiple extraction sources may disagree on the value

### "Review claim expired"

- Claims expire after 30 minutes
- Simply re-claim the review and continue
- Admin can release all expired claims: `POST /v1/faxes/reviews/release-expired`

---

## 19. HIPAA & Security Notes

### Data Access Logging

Every time you view, download, or modify patient data, the system records:
- Your user ID
- What you accessed
- When you accessed it
- Your IP address

This audit trail is immutable and available for compliance reporting.

### Tenant Isolation

You can only access data belonging to your organization (tenant). The system enforces this at every API endpoint.

### Data at Rest

- Documents are stored in MinIO with private access controls
- Database credentials are encrypted
- No patient data is sent to external APIs or cloud services
- All ML models run locally within your infrastructure

### Presigned URLs

Page images in review packets use time-limited presigned URLs that expire after 1 hour. These URLs should not be shared or bookmarked.
