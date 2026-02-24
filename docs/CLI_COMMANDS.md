# CLI Commands & Scripts Reference

Complete reference for all Makefile targets, utility scripts, Docker commands, and operational commands.

---

## Table of Contents

1. [Makefile Commands](#1-makefile-commands)
2. [Utility Scripts](#2-utility-scripts)
3. [Docker Commands](#3-docker-commands)
4. [Service Commands](#4-service-commands)
5. [Celery Worker Commands](#5-celery-worker-commands)
6. [Database Commands](#6-database-commands)
7. [Code Quality Commands](#7-code-quality-commands)
8. [Test Commands](#8-test-commands)
9. [Environment Configuration](#9-environment-configuration)

---

## 1. Makefile Commands

Run from the project root directory:

### Setup

| Command | Description |
|---------|-------------|
| `make help` | Show all available commands |
| `make install` | Install production dependencies from `requirements.txt` |
| `make dev-install` | Install production + dev dependencies (pytest, mypy, ruff, black) |

### Docker Infrastructure

| Command | Description |
|---------|-------------|
| `make docker-up` | Start PostgreSQL, Redis, MinIO via Docker Compose |
| `make docker-down` | Stop all Docker Compose services |
| `make docker-logs` | Tail container logs (all services) |

### Database

| Command | Description |
|---------|-------------|
| `make migrate` | Run Alembic database migrations (upgrade to head) |
| `make migrate-down` | Rollback last migration |

### Services

| Command | Description |
|---------|-------------|
| `make api-ingress` | Start Fax Ingress API on port 8001 (with hot-reload) |
| `make api-review` | Start Fax Review API on port 8002 (with hot-reload) |
| `make celery-worker` | Start Celery worker (fax processing tasks) |
| `make celery-beat` | Start Celery beat scheduler (hourly/weekly tasks) |
| `make all-services` | Start Docker + run migrations (full bootstrap) |

### Quality

| Command | Description |
|---------|-------------|
| `make lint` | Run ruff linter on all code |
| `make format` | Format code with black + isort |
| `make type-check` | Run mypy strict type checking |
| `make test` | Run all tests with verbose output |
| `make test-unit` | Run unit tests only |
| `make test-integration` | Run integration tests only |
| `make test-regression` | Run focused regression tests |
| `make review-fast` | Quick compile check + tests |
| `make review-full` | Full review: lint + format check + type check + all tests |
| `make smoke-api` | Check health endpoints on all 3 APIs |

### Cleanup

| Command | Description |
|---------|-------------|
| `make clean` | Remove `__pycache__`, `.pytest_cache`, `.mypy_cache` |

---

## 2. Utility Scripts

### Download ML Models

```bash
python scripts/download_models.py
```

**Purpose:** Pre-downloads all ML models required by the system:
- PaddleOCR PP-OCRv5 (English detection + recognition + classification)
- LayoutLM Document QA (`impira/layoutlm-document-qa`)
- sentence-transformers (`all-MiniLM-L6-v2`)

**When to run:** First-time setup, or after clearing the model cache. Also run during Docker build to bake models into the image.

**Model cache locations:**
- HuggingFace: `~/.cache/huggingface/` (or `HF_HOME` env var)
- PaddleOCR: `~/.paddleocr/` (or `PADDLEOCR_HOME` env var)

---

### Seed Templates

```bash
python scripts/seed_templates.py
```

**Purpose:** Loads payer-specific templates from sample PDFs into the database. Creates template → version → fields → sample records.

**Prerequisites:**
- Database must be running and migrated
- MinIO must be running
- Template PDFs should be in `pdfs/Templetes_pdf/` directory

**Environment variables:**
- `DATABASE_URL` — PostgreSQL connection string (reads from `.env` file)
- `MINIO_ENDPOINT` — MinIO endpoint (default: `localhost:9000`)
- `SEED_TEMP_DIR` — Alternative temp directory for Windows (default: `D:\temp`)

**What it does:**
1. Reads PDF files from the templates directory
2. Converts each page to a 300 DPI image
3. Computes pHash and ORB features
4. Creates template records in the database
5. Uploads sample images to MinIO
6. Defines field ROI coordinates based on the payer

---

### Generate Training Data

```bash
python scripts/generate_training_data.py --source all
```

**Purpose:** Exports verified human corrections from the `fax_label_example` table into LayoutLM training format.

**Options:**
- `--source all` — Export from all sources (human review + seeded)
- `--source human_review` — Export only human-verified corrections
- `--output-dir` — Output directory (default: `training_data/`)

**Output format:** JSONL files with page images, token positions, and field labels.

---

### Fine-Tune LayoutLM

```bash
python scripts/finetune_layoutlm.py
```

**Purpose:** Fine-tunes a LoRA adapter on top of the base LayoutLM model using collected training data.

**What it does:**
1. Loads training data from exported JSONL files
2. Creates a LoRA adapter configuration (rank=8, alpha=16)
3. Fine-tunes for configurable epochs
4. Saves the adapter to MinIO or local filesystem
5. Registers the new model version in the database

**Key parameters (configured in script):**
- Learning rate: 2e-5
- Batch size: 4
- LoRA rank: 8
- Training epochs: 3

---

### Export Training Data

```bash
python scripts/export_training_data.py
```

**Purpose:** Exports training data labels to disk for external analysis or alternative training pipelines.

---

## 3. Docker Commands

### Full Stack (Development)

```bash
# Build all images
docker compose build

# Start all services
docker compose up -d

# View logs
docker compose logs -f

# View specific service logs
docker compose logs -f fax-worker
docker compose logs -f fax-ingress

# Stop all services
docker compose down

# Stop and remove volumes (WARNING: deletes all data)
docker compose down -v
```

### Infrastructure Only

```bash
# Start just PostgreSQL, Redis, MinIO
docker-compose -f infra/docker-compose.yml up -d

# Stop infrastructure
docker-compose -f infra/docker-compose.yml down
```

### Individual Container Management

```bash
# Restart a specific service
docker compose restart fax-worker

# View container stats
docker stats fax_worker fax_ingress fax_review fax_query

# Execute command in container
docker exec -it fax_worker bash

# View container health
docker inspect --format='{{.State.Health.Status}}' fax_worker
```

### MinIO Console

Access the MinIO web console at `http://localhost:9001`:
- Default credentials: `minioadmin` / `minioadmin123`
- Browse buckets: `fax-documents`, `templates`
- View uploaded files and manage storage

---

## 4. Service Commands

### Starting Services Individually

```bash
# Fax Ingress API (port 8001)
uvicorn services.fax_ingress_api.main:app --host 0.0.0.0 --port 8001 --reload

# Fax Review API (port 8002)
uvicorn services.fax_review_api.main:app --host 0.0.0.0 --port 8002 --reload

# Fax Query API (port 8003)
uvicorn services.fax_query_api.main:app --host 0.0.0.0 --port 8003 --reload
```

### Production Mode (No Reload)

```bash
uvicorn services.fax_ingress_api.main:app --host 0.0.0.0 --port 8001 --workers 4
```

### Health Checks

```bash
# Quick check all services
make smoke-api

# Individual checks
curl http://localhost:8001/health
curl http://localhost:8002/health
curl http://localhost:8003/health
```

---

## 5. Celery Worker Commands

### Starting Workers

```bash
# Standard worker (single-threaded, suitable for ML workloads)
celery -A workers.fax_processing_worker.celery_app worker --loglevel=info --pool=solo

# With concurrency (not recommended for ML due to memory)
celery -A workers.fax_processing_worker.celery_app worker --loglevel=info --concurrency=2

# Beat scheduler (hourly mismatch monitor, weekly retrain)
celery -A workers.fax_processing_worker.celery_app beat --loglevel=info
```

### Monitoring Workers

```bash
# Ping workers
celery -A workers.fax_processing_worker.celery_app inspect ping

# List active tasks
celery -A workers.fax_processing_worker.celery_app inspect active

# List reserved (queued) tasks
celery -A workers.fax_processing_worker.celery_app inspect reserved

# List registered tasks
celery -A workers.fax_processing_worker.celery_app inspect registered

# Get worker stats
celery -A workers.fax_processing_worker.celery_app inspect stats
```

### Queue Management

```bash
# Purge all pending tasks (WARNING: destructive)
celery -A workers.fax_processing_worker.celery_app purge

# List queue lengths (via Redis)
redis-cli -a changeme LLEN fax_processing
redis-cli -a changeme LLEN default
```

### Registered Tasks

| Task Name | Queue | Schedule |
|-----------|-------|----------|
| `workers.fax_processing_worker.tasks.process_fax.process_fax_task` | fax_processing | On-demand |
| `workers.fax_processing_worker.tasks.mismatch_monitor.aggregate_mismatch_metrics` | default | Every 3600s |
| `workers.fax_processing_worker.tasks.retrain_layoutlm.check_and_retrain_layoutlm` | default | Every 604800s |

---

## 6. Database Commands

### Migrations

```bash
# Run all pending migrations
python -m alembic -c infra/alembic.ini upgrade head

# Rollback last migration
python -m alembic -c infra/alembic.ini downgrade -1

# Show current migration state
python -m alembic -c infra/alembic.ini current

# Show migration history
python -m alembic -c infra/alembic.ini history
```

### Direct Database Access

```bash
# Connect to PostgreSQL
psql -h localhost -U faxadmin -d fax_processor

# Useful queries:
# Count jobs by status
SELECT status, COUNT(*) FROM fax_job GROUP BY status;

# Recent failed jobs
SELECT fax_job_id, original_filename, created_at
FROM fax_job WHERE status = 'FAILED'
ORDER BY created_at DESC LIMIT 10;

# Review queue size
SELECT COUNT(*) FROM fax_review
WHERE submitted_at IS NULL;

# Audit trail for a specific job
SELECT action, resource_type, created_at, user_id
FROM audit_log WHERE resource_id = 'your-job-uuid'
ORDER BY created_at;

# Template inventory
SELECT t.payer_name, t.template_name, v.version_label, v.is_active,
       COUNT(DISTINCT s.sample_id) AS samples,
       COUNT(DISTINCT f.template_field_id) AS fields
FROM fax_template t
JOIN fax_template_version v ON v.template_id = t.template_id
LEFT JOIN fax_template_sample s ON s.template_version_id = v.template_version_id
LEFT JOIN fax_template_field f ON f.template_version_id = v.template_version_id
GROUP BY t.payer_name, t.template_name, v.version_label, v.is_active;
```

### Migration Files

Located in `infra/migrations/`:

| File | Description |
|------|-------------|
| `001_initial_schema.sql` | Core tables, enums, templates, extraction, review, audit |
| `002_indexes.sql` | Performance indexes |
| `003_pgvector.sql` | Vector embedding table (fax_embedding) |
| `004_week2_enums.sql` | Additional enum values |
| `005_mismatch_metric.sql` | Mismatch monitoring table |
| `006_label_example.sql` | Training label table (fax_label_example) |
| `007_model_version.sql` | Model version registry |
| `008_template_config.sql` | Template config column additions |
| `009_hitl_flagged_fields.sql` | HITL flagged_fields JSONB column |
| `010_add_dedup_constraint.sql` | Deduplication constraint on fax_job |

---

## 7. Code Quality Commands

### Linting

```bash
# Check for issues
ruff check libs services workers tests

# Auto-fix issues
ruff check --fix libs services workers tests
```

### Formatting

```bash
# Format code
black libs services workers tests
isort libs services workers tests

# Check formatting without changing files
black --check libs services workers tests
isort --check-only libs services workers tests
```

### Type Checking

```bash
mypy libs services workers
```

### Configuration

All tools are configured in `pyproject.toml`:
- **ruff:** line-length=100, target-version=py311, select E/F/I/N/W/UP/B/C4/SIM
- **black:** line-length=100, target-version=py311
- **isort:** profile=black, line_length=100
- **mypy:** strict=true, plugins=pydantic.mypy

---

## 8. Test Commands

### Running Tests

```bash
# All tests with verbose output
pytest tests/ -v

# Specific test file
pytest tests/test_validators.py -v

# Specific test class
pytest tests/test_cross_field_validator.py::TestDateOrdering -v

# Specific test function
pytest tests/test_field_builder.py::TestBuildField::test_single_template_candidate -v

# With coverage report
pytest tests/ --cov=libs --cov-report=html

# Stop on first failure
pytest tests/ -x

# Show print statements
pytest tests/ -s

# Run tests matching a keyword
pytest tests/ -k "test_valid"
```

### Test Categories

```bash
# Unit tests (fast, no external dependencies)
pytest tests/unit/ -v

# Integration tests (require running services)
pytest tests/integration/ -v

# Regression tests (focused stability checks)
pytest tests/test_route_regressions.py tests/test_stage_extraction_regressions.py -v

# Review workflow tests
pytest tests/test_review_workflow_guards.py -v
```

### Current Test Suite

74 tests passing, 3 skipped, 0 failures:
- `test_validators.py` — 13 tests
- `test_cross_field_validator.py` — 20 tests
- `test_field_builder.py` — 10 tests
- `test_hitl.py` — 11 tests
- `test_review_workflow_guards.py` — 7 tests
- (+ regression and production tests)

---

## 9. Environment Configuration

### Environment Files

| File | Purpose | Used By |
|------|---------|---------|
| `.env` | Local development settings | All services (local) |
| `.env.example` | Template — copy to `.env` | Developers |
| `.env.docker` | Docker Compose overrides | Docker containers |
| `.env.production` | Production settings | Production deployment |

### Key Environment Variables

#### Required

```bash
DATABASE_URL=postgresql+psycopg2://faxadmin:password@localhost:5432/fax_processor
REDIS_URL=redis://:password@localhost:6379/0
CELERY_BROKER_URL=redis://:password@localhost:6379/0
CELERY_RESULT_BACKEND=redis://:password@localhost:6379/1
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=your-secret
SECRET_KEY=your-32-char-minimum-secret-key
```

#### Optional (with defaults)

```bash
# General
ENVIRONMENT=development          # development | staging | production

# OCR
OCR_ENABLE_GPU=false             # true to use CUDA GPU
OCR_DPI_TARGET=300               # Target DPI for page rendering
OCR_LANG=en                      # OCR language

# Template Matching
TEMPLATE_PHASH_THRESHOLD=5       # pHash hamming distance threshold
TEMPLATE_ORB_MIN_MATCHES=20      # Minimum ORB feature matches
TEMPLATE_MATCH_MIN_SCORE=0.75    # Combined match score threshold

# VLM (LayoutLM)
VLM_LAYOUTLM_ENABLED=true       # Enable LayoutLM extraction
VLM_LAYOUTLM_SCORE_MULTIPLIER=0.70  # 0.70=base, 1.20=fine-tuned

# Confidence
CONFIDENCE_AUTO_FINALIZE=0.90   # Auto-complete threshold
CONFIDENCE_FIELD_MIN=0.70       # Field "not_present" threshold
CONFIDENCE_CRITICAL_FIELD_MIN=0.85  # Critical field threshold

# HITL
HITL_ENABLED=true               # Enable field flagging
HITL_DEFAULT_THRESHOLD=0.75     # Non-critical field threshold
HITL_CRITICAL_THRESHOLD=0.85    # Critical field threshold
HITL_MIN_FLAGS_FOR_REVIEW=1     # Min flags to trigger NEEDS_REVIEW

# Feature Flags
ENABLE_VLM=true                 # Enable VLM extraction
ENABLE_VECTOR_SEARCH=true       # Enable Tier 2 semantic search
ENABLE_FEEDBACK_LOOP=true       # Enable feedback-based learning

# API
API_HOST=0.0.0.0
API_PORT=8000
API_DEBUG=false
API_LOG_LEVEL=INFO
API_MAX_UPLOAD_SIZE_MB=50
API_ALLOWED_ORIGINS=http://localhost:3000
```

### Generating Secrets

```bash
# Generate a secure SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"

# Generate a secure Redis password
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
