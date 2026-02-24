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
14. [Debugging & Troubleshooting](#14-debugging--troubleshooting)
15. [Code Quality Standards](#15-code-quality-standards)
16. [Performance Tuning](#16-performance-tuning)

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

# Edit .env — at minimum, verify:
#   DATABASE_URL=postgresql+psycopg2://faxadmin:faxpass123@localhost:5432/fax_processor
#   REDIS_URL=redis://:changeme@localhost:6379/0
#   MINIO_ENDPOINT=localhost:9000
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

Edit `libs/shared/extraction/ocr_label_extractor.py` — add to `LABEL_ALIASES`:
```python
LABEL_ALIASES = {
    "your_new_field": [
        "Your Field Label:",
        "Field Label",
        "YFL:",
    ],
    # ... existing fields
}
```

### Step 3: Add Validation (Optional)

If the field needs special validation, update `libs/shared/extraction/validators.py`.

### Step 4: Add Cross-Field Rules (Optional)

If the field participates in cross-field constraints, update `cross_field_validator.py`.

### Step 5: Update HITL Thresholds (Optional)

Edit `libs/shared/extraction/hitl.py` — add to `FIELD_REVIEW_THRESHOLDS`:
```python
FIELD_REVIEW_THRESHOLDS = {
    "your_new_field": {"threshold": 0.80, "is_critical": False},
    # ... existing fields
}
```

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

SQL migration files: `infra/migrations/001_initial_schema.sql` through `010_add_dedup_constraint.sql`

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

Open http://localhost:9001 — login with `minioadmin` / `minioadmin123`

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

## 14. Debugging & Troubleshooting

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

## 15. Code Quality Standards

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

## 16. Performance Tuning

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
