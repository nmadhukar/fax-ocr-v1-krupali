# Developer Guide — Healthcare Fax OCR Processing System

This guide covers local development setup, project structure walkthrough, how to extend or enhance every module, testing practices, and debugging tips.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Local Development Setup](#2-local-development-setup)
3. [Project Structure Deep Dive](#3-project-structure-deep-dive)
4. [How to Extend Each Module](#4-how-to-extend-each-module)
5. [Adding a New Payer](#5-adding-a-new-payer)
6. [Adding a New Extracted Field](#6-adding-a-new-extracted-field)
7. [Adding a New Pipeline Stage](#7-adding-a-new-pipeline-stage)
8. [Adding a New API Endpoint](#8-adding-a-new-api-endpoint)
9. [Template System — Creating and Managing Templates](#9-template-system--creating-and-managing-templates)
10. [LayoutLM Fine-Tuning Workflow](#10-layoutlm-fine-tuning-workflow)
11. [Testing Guide](#11-testing-guide)
12. [Database Migrations](#12-database-migrations)
13. [Docker Development](#13-docker-development)
14. [Operations Console UI — Front-End Architecture](#14-operations-console-ui--front-end-architecture)
15. [Debugging & Troubleshooting](#15-debugging--troubleshooting)
16. [Code Quality Standards](#16-code-quality-standards)
17. [Performance Tuning](#17-performance-tuning)

---

## 1. Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.11+ | Runtime |
| PostgreSQL | 15+ | Database (must have pgvector extension) |
| Redis | 7+ | Celery broker |
| Docker & Docker Compose | Latest | Infrastructure services |
| Git | Latest | Version control |
| poppler-utils | Latest | PDF to image conversion (system package) |

### System Libraries (Linux/macOS)

```bash
# Ubuntu/Debian
sudo apt-get install -y libgl1 libglib2.0-0 libgomp1 libpq-dev poppler-utils

# macOS
brew install poppler libpq
```

### System Libraries (Windows)

- Install [poppler for Windows](https://github.com/oschwartz10612/poppler-windows/releases)
- Add poppler `bin/` to your PATH
- Install Visual C++ Build Tools for native Python extensions

---

## 2. Local Development Setup

### Step 1: Clone and Create Virtual Environment

```bash
git clone <repository-url>
cd fax_ocr_v1_krupali

python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate
```

### Step 2: Install Dependencies

```bash
# Production dependencies
pip install -r requirements.txt

# Development extras (pytest, mypy, ruff, black)
pip install -e ".[dev]"
```

### Step 3: Start Infrastructure

```bash
# Start PostgreSQL, Redis, MinIO via Docker
make docker-up
# or manually:
docker-compose -f infra/docker-compose.yml up -d
```

### Step 4: Configure Environment

```bash
# Copy the example environment file
cp .env.example .env

# Edit .env — at minimum, set these (all required, no defaults):
#   DATABASE_URL=postgresql+psycopg2://faxadmin:faxpass123@localhost:5432/fax_processor
#   REDIS_URL=redis://:changeme@localhost:6379/0
#   MINIO_ENDPOINT=localhost:9000
#   MINIO_ACCESS_KEY=your-access-key
#   MINIO_SECRET_KEY=your-secret-key
#   SECRET_KEY=your-32-char-secret
```

### Step 5: Run Database Migrations

```bash
make migrate
# or:
python -m alembic -c infra/alembic.ini upgrade head
```

### Step 6: Seed Templates (Optional)

```bash
python scripts/seed_templates.py
```

### Step 7: Download ML Models (First Time Only)

```bash
python scripts/download_models.py
```

### Step 8: Start Services

Open **4 terminal windows**:

```bash
# Terminal 1: Ingress API
make api-ingress
# → uvicorn services.fax_ingress_api.main:app --host 0.0.0.0 --port 8001 --reload

# Terminal 2: Review API
make api-review
# → uvicorn services.fax_review_api.main:app --host 0.0.0.0 --port 8002 --reload

# Terminal 3: Celery Worker
make celery-worker
# → celery -A workers.fax_processing_worker.celery_app worker --loglevel=info

# Terminal 4: Celery Beat (optional — for scheduled tasks)
make celery-beat
```

### Step 9: Verify Setup

```bash
# Health checks
make smoke-api
# or manually:
curl http://localhost:8001/health
curl http://localhost:8002/health
curl http://localhost:8003/health
```

---

## 3. Project Structure Deep Dive

```
fax_ocr_v1_krupali/
│
├── libs/shared/                    # Shared library — ALL business logic lives here
│   ├── config/                     #   Settings (Pydantic v2), payer rules, signatures
│   ├── db/                         #   Database: models, repositories, session management
│   │   ├── models/                 #     SQLAlchemy ORM models (one file per table)
│   │   └── repositories/          #     Data access layer (CRUD + custom queries)
│   ├── extraction/                 #   Field extraction pipeline
│   │   ├── constants.py            #     Canonical CRITICAL_FIELDS frozenset (9 fields)
│   │   ├── template_extractor.py   #     PRIMARY: ROI-based extraction
│   │   ├── ocr_label_extractor.py  #     FALLBACK: regex on OCR text
│   │   ├── layoutlm_extractor.py   #     GAP-FILL: LayoutLM Document QA
│   │   ├── field_builder.py        #     Merge 3 sources + scoring
│   │   ├── canonicalizer.py        #     Normalize field values
│   │   ├── validators.py           #     Type-specific validation
│   │   ├── cross_field_validator.py#     Cross-field constraints
│   │   ├── hitl.py                 #     HITL flagging logic
│   │   └── output_formatter.py     #     Client-facing response format
│   ├── template/                   #   Template matching (pHash + ORB)
│   ├── ocr/                        #   OCR abstraction (PaddleOCR)
│   ├── vlm/                        #   VLM abstraction (LayoutLM)
│   ├── embeddings/                 #   Vector embedding client
│   ├── security/                   #   Auth, audit, file validation, rate limiting
│   ├── storage/                    #   MinIO/S3 adapter
│   ├── scoring/                    #   Confidence aggregation
│   ├── monitoring/                 #   Pipeline metrics
│   └── feedback/                   #   Feedback analysis for threshold tuning
│
├── services/                       # 3 FastAPI services (thin routing layer)
│   ├── fax_ingress_api/            #   Port 8001: Upload, status, results
│   ├── fax_review_api/             #   Port 8002: Templates, review, analytics, models
│   │   └── ui/                     #     Operations Console (browser SPA)
│   │       ├── index.html          #       HTML structure (~834 lines)
│   │       ├── app.js              #       Application logic (~1136 lines)
│   │       └── styles.css          #       Styling & design system (~1116 lines)
│   └── fax_query_api/              #   Port 8003: Query results
│
├── workers/                        # Celery background workers
│   └── fax_processing_worker/
│       ├── celery_app.py           #   Celery configuration + beat schedule
│       └── tasks/
│           ├── process_fax.py      #   Main 17-stage pipeline
│           ├── mismatch_monitor.py #   Hourly anomaly detection
│           ├── retrain_layoutlm.py #   Weekly model fine-tuning
│           └── stages/             #   Pipeline stage modules
│
├── scripts/                        # Utility scripts
├── tests/                          # Test suite
├── configs/                        # YAML configuration (payer_rules.yml)
├── infra/                          # Infrastructure (migrations, docker-compose)
└── docs/                           # Documentation (this guide)
```

### Key Design Pattern: Thin Services, Fat Library

- **Services** are thin routing layers — they define FastAPI endpoints and delegate to `libs/shared/`
- **libs/shared/** contains ALL business logic, models, repositories, and utilities
- **Workers** orchestrate the pipeline by calling into `libs/shared/` modules
- This means you can test extraction logic without starting an API server

---

## 4. How to Extend Each Module

### 4.1 Adding a New OCR Engine

1. Create `libs/shared/ocr/your_engine.py`:
   ```python
   from libs.shared.ocr.base import OcrClient, OcrResult

   class YourOcrClient(OcrClient):
       def extract(self, image: np.ndarray) -> OcrResult:
           # Your implementation
           pass
   ```

2. Update `settings.py` to add configuration
3. Update `process_fax.py` to instantiate your client based on settings

### 4.2 Adding a New VLM

1. Create `libs/shared/vlm/your_model.py`:
   ```python
   from libs.shared.vlm.base import VlmClient, VlmConfig

   class YourVlmClient(VlmClient):
       def __init__(self, config: VlmConfig):
           # Load your model
           pass

       def extract_field(self, image, question: str) -> dict:
           # Your implementation
           pass
   ```

2. Create `libs/shared/extraction/your_extractor.py` that wraps it
3. Add it as Source 4 in the `FieldBuilder` merge

### 4.3 Adding a New Storage Backend

1. Create `libs/shared/storage/your_adapter.py`:
   ```python
   from libs.shared.storage.base import StorageAdapter

   class YourStorageAdapter(StorageAdapter):
       def upload(self, key: str, data: bytes, content_type: str, **kwargs): ...
       def download(self, key: str) -> bytes: ...
       def delete(self, key: str): ...
       def get_url(self, key: str, expires_in: int = 3600) -> str: ...
   ```

2. Update `get_storage()` dependency in route files

### 4.4 Adding a New Cross-Field Validation Rule

1. Add method to `CrossFieldValidator` in `libs/shared/extraction/cross_field_validator.py`:
   ```python
   def _check_your_rule(self, fields: dict, result: CrossFieldResult) -> None:
       # Your validation logic
       value = self._get_value(fields, "your_field")
       if value and some_condition:
           result.errors.append("Your error message")
   ```

2. Call it from `validate()`:
   ```python
   self._check_your_rule(fields, result)
   ```

3. Add tests in `tests/test_cross_field_validator.py`

### 4.5 Adding a New Field Validator

1. Add to `libs/shared/extraction/validators.py`:
   ```python
   def validate_your_type(value: str | None) -> ValidationResult:
       # Your validation logic
       pass
   ```

2. Update `_infer_field_type()` to recognize your field name pattern
3. Add tests in `tests/test_validators.py`

---

## 5. Adding a New Payer

### Step 1: Add to Enum

Edit `libs/shared/db/models/enums.py`:
```python
class PayerNameEnum(str, enum.Enum):
    # ... existing payers
    YOUR_PAYER = "YOUR_PAYER"
```

### Step 2: Add Database Migration

Create `infra/migrations/0XX_add_your_payer.sql`:
```sql
ALTER TYPE payer_name_enum ADD VALUE IF NOT EXISTS 'YOUR_PAYER';
```

### Step 3: Add Payer Rules

Edit `configs/payer_rules.yml`:
```yaml
  YOUR_PAYER:
    display_name: "Your Payer Name"
    critical_fields:
      - member_id
      - prior_auth_number
      - decision

    field_validations:
      member_id:
        regex: '^[A-Z0-9]{8,14}$'
        description: "8-14 alphanumeric characters"

    confidence_thresholds:
      member_id: 0.88
      prior_auth_number: 0.85

    canonicalization:
      member_id:
        uppercase: true
        remove_spaces: true
```

### Step 4: Create Template

Use the Review API to create a template, version, upload sample, and define fields:

```bash
# 1. Create template
curl -X POST http://localhost:8002/v1/templates \
  -H "Content-Type: application/json" \
  -d '{"payer_name": "YOUR_PAYER", "doc_type": "PRIOR_AUTH_FORM", "template_name": "Standard PA Form"}'

# 2. Create version
curl -X POST http://localhost:8002/v1/templates/{template_id}/versions \
  -d '{"version_label": "v1.0"}'

# 3. Upload sample image
curl -X POST http://localhost:8002/v1/templates/versions/{version_id}/samples \
  -F "file=@sample.png"

# 4. Define fields with ROI coordinates
curl -X POST http://localhost:8002/v1/templates/versions/{version_id}/fields \
  -d '{"field_key": "member_id", "roi_x0": 0.05, "roi_y0": 0.15, "roi_x1": 0.45, "roi_y1": 0.19}'

# 5. Activate the version
curl -X POST http://localhost:8002/v1/templates/versions/{version_id}/activate
```

### Step 5: Seed via Script (Alternative)

Add your template PDFs to `pdfs/Templetes_pdf/your_payer/` and update `scripts/seed_templates.py`.

---

## 6. Adding a New Extracted Field

### Step 1: Define Field in Templates

Add field ROI definition to relevant template versions via the API or `seed_templates.py`.

### Step 2: Add Label Alias (for OCR Label Extractor)

Edit `libs/shared/extraction/ocr_label_extractor.py` — add to `LABEL_ALIASES` (currently 16 fields):
```python
LABEL_ALIASES = {
    "your_new_field": [
        "Your Field Label:",
        "Field Label",
        "YFL:",
    ],
    # ... existing 16 fields:
    # member_id, prior_auth_number, patient_name, patient_dob,
    # provider_name, provider_npi, provider_phone, provider_fax,
    # auth_effective_date, auth_expiration_date, service_code,
    # diagnosis_codes, units_requested, next_review_date,
    # insurance_rep_name, insurance_rep_phone
}
```

If the field is phone-type, also add it to the phone filter in the same file (search for `"provider_phone", "provider_fax", "insurance_rep_phone"`) and to the phone detection in `workers/fax_processing_worker/tasks/stages/validation.py`.

If the field should have a max character length, add it to `_MAX_LENGTHS` dict in the same file.

### Step 2b: Add Metadata-Sourced Field (Alternative)

If the field value comes from job metadata (not OCR), add it to `libs/shared/extraction/output_formatter.py` → `format_summary()`. The function accepts optional keyword args for metadata fields (currently `payer_name` and `fax_received_date`) and injects them with `source: "SYSTEM"` and `confidence: 1.0`. Then pass the value from the API route.

### Step 3: Add Validation (Optional)

If the field needs special validation, update `libs/shared/extraction/validators.py`.

### Step 4: Add Cross-Field Rules (Optional)

If the field participates in cross-field constraints, update `cross_field_validator.py`.

### Step 5: Update HITL Thresholds (Optional)

Non-critical fields automatically use the default threshold (0.75). Only add to `libs/shared/extraction/constants.py` → `CRITICAL_FIELDS` if the field has direct patient-safety or billing impact (this raises its threshold to 0.85 and gives it 2x weight in scoring).

### Step 6: Add Payer-Specific Rules

Update `configs/payer_rules.yml` to add validation rules for the new field per payer.

---

## 7. Adding a New Pipeline Stage

### Step 1: Create Stage Module

Create `workers/fax_processing_worker/tasks/stages/your_stage.py`:

```python
"""Your stage description."""

import logging
from . import PipelineContext

logger = logging.getLogger(__name__)


def your_step(ctx: PipelineContext) -> None:
    """Description of what this step does."""
    logger.info("[Stage X] Starting your_step [job=%s]", ctx.fax_job_id[:8])

    # Access pipeline context:
    # ctx.db           — SQLAlchemy session
    # ctx.settings     — Application settings
    # ctx.job          — FaxJob model instance
    # ctx.pages        — List of (FaxPage, np.ndarray) tuples
    # ctx.ocr_results  — Dict[page_num, OcrResult]
    # ctx.candidates_by_field — Dict[field_key, List[ExtractionCandidate]]
    # ctx.extracted_fields — Dict[field_key, FieldResult]

    # Your logic here...

    logger.info("[Stage X] Completed your_step [job=%s]", ctx.fax_job_id[:8])
```

### Step 2: Register in Pipeline

Edit `workers/fax_processing_worker/tasks/process_fax.py`:

```python
from .stages import your_stage

# Add to the pipeline sequence:
your_stage.your_step(ctx)
```

### Step 3: Update Stage Init

Edit `workers/fax_processing_worker/tasks/stages/__init__.py` to export your module.

---

## 8. Adding a New API Endpoint

### Step 1: Choose the Right Service

- **Ingress API** (8001) — Upload/status/results
- **Review API** (8002) — Templates, review workflow, analytics, model management
- **Query API** (8003) — Read-only queries

### Step 2: Create Route

Example — adding an endpoint to Review API:

```python
# services/fax_review_api/api/v1/routes/your_routes.py

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from libs.shared.db.session import get_db
from libs.shared.security.auth import AuthUser, get_current_user

router = APIRouter()

@router.get("/your-endpoint")
def your_endpoint(
    user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Your endpoint description."""
    # Your logic
    return {"result": "data"}
```

### Step 3: Register Router

Edit `services/fax_review_api/main.py`:
```python
from services.fax_review_api.api.v1.routes import your_routes

app.include_router(your_routes.router, prefix="/v1/your-prefix", tags=["YourTag"])
```

### Important Patterns

- Always use `Depends(get_current_user)` for authentication
- Always use `Depends(get_db)` for database access
- Use sync `def` (not `async def`) — SQLAlchemy sessions are synchronous
- Add audit logging for any PHI access
- Enforce tenant isolation

---

## 9. Template System — Creating and Managing Templates

### Template Hierarchy

```
Template (payer + doc_type + name)
  └── Version (matching thresholds, active flag)
        ├── Sample (page image + computed pHash + ORB features)
        └── Field (field_key + ROI coordinates + validation)
```

### ROI Coordinate System

All coordinates are **normalized to [0.0, 1.0]** relative to the page dimensions:

```
(0,0) ────────────────────────── (1,0)
  │                                 │
  │    ┌─────────────────┐          │
  │    │  roi_x0, roi_y0 │          │
  │    │    (top-left)    │          │
  │    │                  │          │
  │    │   roi_x1, roi_y1 │         │
  │    │   (bottom-right)  │        │
  │    └───────────────────┘        │
  │                                 │
(0,1) ────────────────────────── (1,1)
```

### Using suggest-roi

The `POST /v1/templates/suggest-roi` endpoint helps you define ROI coordinates:

1. Upload a fax through the Ingress API
2. Wait for OCR processing to complete
3. Call suggest-roi with the job ID, page number, and field key
4. The system finds OCR tokens matching the field and returns a suggested bounding box

### Test-Match and Test-Extract

- `POST /v1/templates/test-match` — Upload an image to test template matching
- `POST /v1/templates/versions/{id}/test-extract` — Upload an image to test ROI extraction

---

## 10. LayoutLM Fine-Tuning Workflow

### Overview

The system automatically collects training data from human review corrections and periodically fine-tunes a LoRA adapter for LayoutLM.

### Manual Workflow

```bash
# 1. Export training data from verified corrections
python scripts/generate_training_data.py --source all

# 2. Fine-tune LayoutLM adapter
python scripts/finetune_layoutlm.py

# 3. Register the new model version
curl -X POST http://localhost:8002/v1/models \
  -d '{"model_type": "LAYOUTLM", "version_tag": "layoutlm-v1.1", "model_path": "/path/to/adapter"}'

# 4. Update metrics after evaluation
curl -X PUT http://localhost:8002/v1/models/{id}/metrics \
  -d '{"metrics": {"accuracy": 0.92, "f1": 0.89}}'

# 5. Promote to production
curl -X POST http://localhost:8002/v1/models/{id}/promote \
  -d '{"promoted_by": "admin"}'
```

### Automatic Workflow

Celery beat runs `check_and_retrain_layoutlm` weekly:
1. Checks if enough new training labels exist (threshold: configurable)
2. Exports training data
3. Fine-tunes LoRA adapter
4. Registers new version
5. Compares metrics against active version
6. Auto-promotes if accuracy improves

### Adjusting VLM Score Multiplier

After fine-tuning, increase the VLM score multiplier to trust LayoutLM more:

```bash
# In .env:
VLM_LAYOUTLM_SCORE_MULTIPLIER=1.20  # was 0.70 for base model
```

---

## 11. Testing Guide

### Running Tests

```bash
# All tests
make test
# or:
pytest tests/ -v

# Unit tests only
make test-unit

# Integration tests only
make test-integration

# Regression tests
make test-regression

# Quick compile + test
make review-fast

# Full quality check (lint + type-check + test)
make review-full
```

### Test Structure

| File | Tests |
|------|-------|
| `test_validators.py` | Field-level validation (dates, phone, NPI, SSN) |
| `test_cross_field_validator.py` | Cross-field constraint rules |
| `test_field_builder.py` | Multi-source field merging and scoring |
| `test_hitl.py` | HITL flagging thresholds and correction logic |
| `test_review_workflow_guards.py` | Review claim/submit state machine |
| `test_route_regressions.py` | API endpoint regression tests |
| `test_stage_extraction_regressions.py` | Pipeline stage regressions |
| `test_production_live.py` | End-to-end with real services |

### Writing Tests

```python
import pytest
from libs.shared.extraction.your_module import YourClass

class TestYourFeature:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.instance = YourClass()

    def test_basic_case(self):
        result = self.instance.do_something(input_data)
        assert result.is_valid
        assert not result.errors

    def test_edge_case(self):
        result = self.instance.do_something(edge_input)
        assert any("expected" in e.lower() for e in result.errors)
```

### Test Fixtures

See `tests/conftest.py` for shared fixtures:
- Database session mocks
- MinIO stubs
- Sample extraction data generators

---

## 12. Database Migrations

### Location

SQL migration files: `infra/migrations/001_initial_schema.sql` through `014_bigint_pk_columns.sql`

### Creating a New Migration

```bash
# Create a new migration file
touch infra/migrations/011_your_migration.sql
```

Example migration:
```sql
-- 011_your_migration.sql
-- Description: Add your_column to fax_job table

ALTER TABLE fax_job ADD COLUMN your_column VARCHAR(255);
CREATE INDEX idx_fax_job_your_column ON fax_job(your_column);
```

### Running Migrations

```bash
make migrate
# or:
python -m alembic -c infra/alembic.ini upgrade head
```

### Rolling Back

```bash
make migrate-down
```

---

## 13. Docker Development

### Building

```bash
docker compose build
```

### Full Stack

```bash
docker compose up -d
```

This starts: PostgreSQL, Redis, MinIO, bucket initializer, 3 API services, Celery worker, Celery beat.

### Viewing Logs

```bash
docker compose logs -f fax-worker    # Worker logs
docker compose logs -f fax-ingress   # Ingress API logs
docker compose logs -f fax-review    # Review API logs
```

### Accessing MinIO Console

Open http://localhost:9001 — login with the credentials configured via `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` in your env file (**required** — no defaults)

### Memory Requirements

| Service | Min Memory | Recommended |
|---------|-----------|-------------|
| PostgreSQL | 256MB | 512MB |
| Redis | 64MB | 128MB |
| MinIO | 128MB | 256MB |
| API Services | 256MB each | 512MB each |
| **Celery Worker** | **2GB** | **4GB** |

The worker needs the most memory because it loads PaddleOCR (~100MB), LayoutLM (~130MB), and sentence-transformers (~90MB) into RAM.

---

## 14. Operations Console UI — Front-End Architecture

The Operations Console is a single-page application served by the Review API at `/ui`. It provides a professional browser-based interface for all system workflows.

### File Structure

```
services/fax_review_api/ui/
├── index.html    # ~834 lines — Main HTML document, all markup and structure
├── app.js        # ~1136 lines — Complete application logic (vanilla JavaScript)
└── styles.css    # ~1116 lines — Full styling with CSS custom properties
```

All three files are served as static assets by FastAPI's `StaticFiles` middleware, mounted at `/ui` with `html=True` in `services/fax_review_api/main.py`.

### Technology Choices

| Aspect | Choice | Rationale |
|--------|--------|-----------|
| **Framework** | None (vanilla JS) | Zero build step, zero dependencies, instant loading |
| **Module pattern** | IIFE (`(function() { ... })()`) | Prevents global scope pollution |
| **State management** | Single `state` object + `localStorage` | Simple, no library overhead |
| **CSS architecture** | CSS custom properties (`:root` variables) | Consistent theming, easy to modify |
| **Typography** | Sora (sans) + IBM Plex Mono (mono) | Professional healthcare aesthetic |
| **Layout** | CSS Grid + Flexbox | Responsive, no framework needed |
| **API communication** | `fetch()` API | Native browser, no Axios/jQuery dependency |

### Application Architecture

#### State Management

```javascript
const state = {
  profile: { ...DEFAULT_PROFILE },  // Connection settings (persisted to localStorage)
  reviewPacket: null,                // Currently loaded review packet data
};
```

The `profile` object contains API base URLs, JWT token, and reviewer ID. It is hydrated from `localStorage` on page load via `hydrateProfile()` and persisted on save via `persistProfile()`. Both operations are wrapped in try/catch to handle private browsing mode and storage quota exceeded errors.

#### Initialization Flow

On `DOMContentLoaded`, the `init()` function runs:

1. `hydrateProfile()` — Load saved profile from `localStorage`
2. `applyProfileToInputs()` — Populate form fields with saved values
3. `bindTabs()` — Attach WAI-ARIA tab navigation with `aria-selected` state
4. `bindConfigPanel()` — Wire save/reset/health-test buttons
5. `bindDashboard()` — Wire the Dashboard tab (analytics charts, KPI refresh)
6. `bindWorkflow()` — Wire all Workflow tab forms and event handlers
7. `bindTemplates()` — Wire all Templates tab forms and event handlers
8. `bindIntelligence()` — Wire Intelligence tab (query, analytics, models)
9. `bindApiConsole()` — Wire the raw API console form
9. `animateReveals()` — Set staggered animation delays on `.reveal` elements
10. `syncStatusPills()` — Update environment/auth status pills in the hero

#### API Communication Layer

All API calls go through a single `apiRequest(service, path, options)` function:

```javascript
apiRequest("review", "/v1/faxes/reviews/unclaimed", {
  method: "GET",
  query: { limit: 100, skip: 0 },
});
```

The function:
- Resolves the base URL from the connection profile based on `service` name
- Constructs the full URL with query parameters using the `URL` API
- Attaches the JWT Bearer token if configured
- Handles `FormData` (for file uploads) and JSON bodies
- Parses the response as JSON (with fallback to raw text)
- Throws enriched error objects with `status`, `serviceName`, and `payload` for error handling

#### Button Busy State Pattern

All async operations use `withButtonBusy(button, busyLabel, action, errorOutputElement)`:

1. Disables the button and changes its text to the busy label
2. Executes the async action
3. On error, renders error details to the output element and shows a toast
4. Re-enables the button and restores original text in `finally`

Errors are caught and displayed — they do not re-throw, so the user always gets feedback.

### Security Measures

| Measure | Implementation |
|---------|---------------|
| **XSS prevention** | All dynamic content uses `escapeHtml()` and `escapeAttr()` before innerHTML injection |
| **URL path encoding** | All URL path parameters use `encId()` → `encodeURIComponent(String(id))` to prevent path traversal |
| **localStorage safety** | `hydrateProfile()` and `persistProfile()` wrapped in try/catch |
| **Input sanitization** | All user inputs are `.trim()`-ed before use in API calls |
| **Content-Type** | JSON bodies set `Content-Type: application/json`; file uploads use native `FormData` |

### Accessibility (WCAG 2.1 AA)

| Feature | Implementation |
|---------|---------------|
| **Tab navigation** | WAI-ARIA Tabs pattern: `role="tablist"`, `role="tab"`, `role="tabpanel"`, `aria-selected`, `aria-controls`, `aria-labelledby` |
| **Focus indicators** | `:focus-visible` outlines on all buttons, inputs, selects, and textareas |
| **Live regions** | `aria-live="polite"` on the activity feed and toast notification host |
| **Lightbox** | Image viewer dialog with `role="dialog"`, `aria-modal="true"`, `aria-label` |
| **Dynamic content** | Correction inputs and action buttons include descriptive `aria-label` |
| **Reduced motion** | `@media (prefers-reduced-motion: reduce)` disables all animations |
| **Color contrast** | All text colors meet WCAG AA contrast ratio (e.g., `--warn: #9a6003` on light background) |
| **Print styles** | `@media print` hides decorative elements, removes backdrops, forces single-column layout |

### CSS Design System

The stylesheet uses CSS custom properties for consistent theming:

```css
:root {
  --bg-0: #eef3f2;           /* Page background gradient start */
  --bg-1: #f8f6ef;           /* Page background gradient end */
  --panel: rgba(255,255,255,0.86); /* Glass-morphism panel */
  --accent: #0f766e;         /* Primary teal accent */
  --accent-strong: #0b5f59;  /* Hover state for accent */
  --accent-soft: #d7f3ef;    /* Soft accent backgrounds */
  --warn: #9a6003;           /* Warning text (WCAG AA compliant) */
  --danger: #b42318;         /* Error/danger text */
  --success: #12804a;        /* Success text */
  --radius: 16px;            /* Standard border radius */
  --radius-sm: 12px;         /* Small border radius */
  --shadow: 0 18px 40px rgba(10,26,36,0.08); /* Card shadow */
}
```

#### Responsive Breakpoints

| Breakpoint | Layout Change |
|------------|---------------|
| `> 1200px` | Two-column grid: 330px sidebar + fluid workspace |
| `860px–1200px` | Single-column: sidebar stacks above workspace |
| `< 860px` | Hero stacks vertically, forms go single-column, tabs scroll horizontally |

### How to Extend the UI

#### Adding a New Tab

1. Add a tab button in `index.html` inside the `nav.tabs` element:
   ```html
   <button class="tab" type="button" data-tab="newtab" id="tab-btn-newtab"
           role="tab" aria-selected="false" aria-controls="tab-newtab">New Tab</button>
   ```

2. Add the tab panel section:
   ```html
   <section id="tab-newtab" class="tab-panel" role="tabpanel" aria-labelledby="tab-btn-newtab">
     <article class="card reveal">
       <h2>New Feature</h2>
       <!-- Your content here -->
     </article>
   </section>
   ```

3. Create a `bindNewTab()` function in `app.js` and call it from `init()`.

Tab switching is handled automatically by `bindTabs()` — no additional JavaScript is needed for navigation. The existing tabs are: Dashboard, Workflow, Templates, Intelligence, and API Console.

#### Adding a New API Call

Use the existing patterns:

```javascript
$("myForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  await withButtonBusy(event.submitter, "Loading...", async () => {
    const response = await apiRequest("review", "/v1/my-endpoint", {
      method: "POST",
      body: { key: $("myInput").value.trim() },
    });
    renderJson($("myOutput"), response.data);
    toast("Operation complete.", "success");
  }, $("myOutput"));
});
```

#### Adding a New Data Table

Use the existing table rendering pattern:

```javascript
function renderMyTable(items) {
  $("myTableBody").innerHTML = items.map((item) => `
    <tr>
      <td>${escapeHtml(item.name)}</td>
      <td>${escapeHtml(item.value)}</td>
    </tr>
  `).join("");
}
```

Always use `escapeHtml()` for text content and `escapeAttr()` for HTML attribute values.

### Key Utility Functions

| Function | Purpose |
|----------|---------|
| `escapeHtml(value)` | Escape `&`, `<`, `>`, `"`, `'` for safe innerHTML |
| `escapeAttr(value)` | Alias for `escapeHtml()` — use for HTML attributes |
| `encId(id)` | `encodeURIComponent(String(id))` — safe URL path params |
| `shortId(id)` | Truncate UUID to first 8 characters for display |
| `renderJson(element, value)` | Pretty-print JSON into a `<pre>` block |
| `toast(message, kind)` | Show floating notification (`success`, `error`, `info`) |
| `addActivity(message, kind)` | Prepend timestamped entry to the activity feed |
| `withButtonBusy(btn, label, fn, errEl)` | Disable button during async operation, handle errors |
| `apiRequest(service, path, opts)` | Unified API communication with auth, error handling |
| `parseJsonText(text, errMsg)` | Parse JSON with user-friendly error message on failure |

---

## 15. Debugging & Troubleshooting

### Common Issues

**"PaddlePaddle DLL conflict with PyTorch on Windows"**
- Solution: Import torch BEFORE PaddlePaddle (already handled in `process_fax.py`)

**"LayoutLM model loading takes 30-120 seconds"**
- Expected on first task. The model is loaded once as a singleton and reused across tasks.

**"File has not been read yet" (Edit tool error)**
- This is a Claude Code tool limitation — re-read the file before editing.

**"celery worker not receiving tasks"**
- Check Redis connection: `redis-cli -a changeme ping`
- Check queue name: task must be sent to `fax_processing` queue
- Check broker URL matches between API and worker

**"Template matching fails on all documents"**
- Verify template samples are uploaded at 300 DPI
- Check pHash threshold (lower = stricter): default is 5
- Try `POST /v1/templates/test-match` to debug matching scores

**"OCR returns empty text"**
- Check image quality: PaddleOCR needs minimum ~100 DPI
- Verify preprocessing: deskew and contrast enhancement should be applied
- Try `OCR_DET_DB_THRESH=0.2` for more aggressive text detection

### Enabling Debug Logging

```bash
# In .env:
API_LOG_LEVEL=DEBUG
API_DEBUG=true
```

### Checking Worker Health

```bash
celery -A workers.fax_processing_worker.celery_app inspect ping
celery -A workers.fax_processing_worker.celery_app inspect active
celery -A workers.fax_processing_worker.celery_app inspect reserved
```

---

## 16. Code Quality Standards

### Tools

| Tool | Config | Command |
|------|--------|---------|
| **ruff** | `pyproject.toml [tool.ruff]` | `ruff check libs services workers tests` |
| **black** | `pyproject.toml [tool.black]` | `black libs services workers tests` |
| **isort** | `pyproject.toml [tool.isort]` | `isort libs services workers tests` |
| **mypy** | `pyproject.toml [tool.mypy]` | `mypy libs services workers` |

### Standards

- Line length: 100 characters
- Python target: 3.11
- Import order: isort with "black" profile
- Type annotations: strict mode (mypy)
- Docstrings: Google style
- Testing: pytest with asyncio_mode="auto"

### Pre-Commit

```bash
pip install pre-commit
pre-commit install
```

---

## 17. Performance Tuning

### OCR Performance

- `OCR_ENABLE_GPU=true` — Use CUDA GPU for PaddleOCR (requires NVIDIA GPU)
- `OCR_REC_BATCH_NUM=6` — Batch size for text recognition
- `OCR_MAX_BATCH_SIZE=10` — Maximum batch size

### Database Performance

- `DB_POOL_SIZE=10` — Connection pool size (increase for high concurrency)
- `DB_MAX_OVERFLOW=20` — Additional connections beyond pool_size
- Indexes are defined in `infra/migrations/002_indexes.sql`

### Worker Performance

- `--pool=solo` — Single-threaded (default for ML workloads)
- `--concurrency=1` — One task at a time (prevents memory exhaustion with ML models)
- Memory limit: 4GB in Docker Compose

### Template Matching Performance

- `TEMPLATE_ORB_N_FEATURES=500` — Reduce for faster matching, increase for accuracy
- `TEMPLATE_PHASH_THRESHOLD=5` — Lower = stricter = fewer candidates to check
- `TEMPLATE_LOWE_RATIO=0.7` — Lowe's ratio test threshold for ORB matching
