# Technical Reference

This document is intended for developers working on or integrating with the Healthcare Fax Processing System. It covers the codebase structure, key algorithms, model details, configuration system, extension points, and operational considerations.

---

## Technology Stack

| Component | Technology | Version | Purpose |
|---|---|---|---|
| Runtime | Python | 3.10 | Core language |
| Web framework | FastAPI | 0.110+ | All three APIs |
| Task queue | Celery | 5.3+ | Async pipeline processing |
| Queue broker | Redis | 7 | Celery broker and result backend |
| Database ORM | SQLAlchemy | 2.0 | Async-compatible ORM |
| Database | PostgreSQL | 15 + pgvector | Primary store |
| File store | MinIO | Latest | S3-compatible object storage |
| OCR engine | PaddleOCR PP-OCRv5 | 2.8+ | Text extraction from images |
| PDF rendering | pdf2image / poppler | — | PDF to image conversion |
| Template matching | pHash (dHash) | — | Perceptual image hashing |
| Document QA | LayoutLM | via HuggingFace | Field extraction model |
| Image processing | OpenCV, Pillow | — | Preprocessing and manipulation |
| Configuration | Pydantic Settings | 2.x | Environment-based config |
| Auth | PyJWT | 2.x | JWT token signing/verification |
| Container | Docker / Docker Compose | 24+ | Deployment |

---

## Repository Structure

```
fax_ocr/
|
+-- docker-compose.yml           All services defined here
+-- Dockerfile                   Multi-stage build (builder + runtime)
+-- docker-entrypoint.sh         Startup script: wait, migrate, seed, exec
+-- pyproject.toml               Package metadata and optional dev extras
+-- requirements.txt             All Python dependencies
+-- Makefile                     Developer convenience targets
+-- .env                         Local dev settings (localhost URLs)
+-- .env.docker                  Docker settings (Docker internal hostnames)
+-- .env.production              Production template (fill before deploying)
|
+-- configs/
|   +-- payer_rules.yml          Per-payer field validation rules
|
+-- infra/
|   +-- migrations/              SQL migration files (001 through 009)
|   +-- init-scripts/            PostgreSQL extension setup (runs on first container start)
|   +-- docker-compose.dev.yml   Dev-only infra compose (no application containers)
|   +-- alembic.ini              Alembic config (not used in current setup)
|
+-- services/
|   +-- fax_ingress_api/
|   |   +-- main.py              FastAPI app factory and lifespan
|   |   +-- api/v1/routes/
|   |       +-- faxes.py         Upload, status, results, list, OCR, delete
|   |       +-- auth.py          Login endpoint
|   |
|   +-- fax_review_api/
|   |   +-- main.py
|   |   +-- api/v1/routes/
|   |       +-- review.py        Review queue, claim, submit
|   |       +-- templates.py     Template CRUD and field definitions
|   |       +-- analytics.py     Summary, payer breakdown, field accuracy
|   |       +-- models.py        Model version registry
|   |
|   +-- fax_query_api/
|       +-- main.py
|       +-- api/v1/routes/
|           +-- query.py         Read-only job and results queries
|
+-- workers/
|   +-- fax_processing_worker/
|       +-- celery_app.py        Celery application and task routing
|       +-- tasks/
|           +-- process_fax.py   Main 17-stage pipeline
|           +-- retrain_layoutlm.py  Scheduled retraining task
|
+-- libs/shared/
|   +-- config/
|   |   +-- settings.py          All settings classes (ApiSettings, OCRSettings, etc.)
|   |
|   +-- db/
|   |   +-- models/
|   |   |   +-- fax_job.py
|   |   |   +-- fax_page.py
|   |   |   +-- fax_ocr_token.py
|   |   |   +-- fax_template.py
|   |   |   +-- fax_extraction.py
|   |   |   +-- fax_review_feedback.py
|   |   |   +-- fax_label_example.py
|   |   |   +-- model_version.py
|   |   |   +-- audit_log.py
|   |   |   +-- enums.py          All database enum types
|   |   |
|   |   +-- repositories/
|   |       +-- fax_job_repo.py
|   |       +-- fax_page_repo.py
|   |       +-- ocr_token_repo.py
|   |       +-- template_repo.py
|   |       +-- extraction_repo.py
|   |       +-- label_repo.py
|   |
|   +-- storage/
|   |   +-- minio_client.py      MinIO adapter (upload, download, presign)
|   |
|   +-- ocr/
|   |   +-- paddle_client.py     PaddleOCR subprocess wrapper
|   |
|   +-- template/
|   |   +-- matcher.py           pHash matching and anchor verification
|   |   +-- phash.py             pHash computation, signed/unsigned conversion
|   |
|   +-- extraction/
|   |   +-- template_extractor.py    Label-anchored field extraction
|   |   +-- ocr_label_extractor.py  Regex-based label matching
|   |   +-- layoutlm_extractor.py   LayoutLM gap-fill extractor
|   |   +-- field_builder.py        Multi-source merge and scoring
|   |   +-- output_formatter.py     Client-facing format conversion
|   |   +-- field_type_validator.py Field type validation rules
|   |   +-- hitl.py                 HITL flag computation and correction application
|   |
|   +-- vlm/
|   |   +-- base.py             VlmClient abstract base, VlmConfig, VlmResponse
|   |   +-- layoutlm_client.py  LayoutLM Document QA client
|   |
|   +-- classification/
|   |   +-- document_splitter.py    Document type classification
|   |   +-- cover_detector.py       Cover page detection utilities
|   |
|   +-- monitoring/
|   |   +-- pipeline_metrics.py     Confidence scoring and metric aggregation
|   |
|   +-- utils/
|       +-- image_utils.py          Deskew, denoise, binarize, resize
|       +-- pdf_utils.py            PDF splitting and DPI rendering
|       +-- auth.py                 JWT encode/decode, dependency injection
|       +-- rate_limiter.py         Token-bucket rate limiter
|
+-- scripts/                    Operational scripts (run against live system)
|   +-- seed_templates.py
|   +-- generate_training_data.py
|   +-- finetune_layoutlm.py
|   +-- export_training_data.py
|   +-- do_upload.py
|   +-- start_services.ps1
|
+-- tests/
|   +-- unit/                   339 passing tests
|   +-- integration/
|
+-- pdfs/
    +-- Templetes_pdf/          8 payer template source PDFs
    +-- Client_response_for_templete/
```

---

## Configuration System

All settings are managed through `libs/shared/config/settings.py` using Pydantic `BaseSettings`. Settings are grouped into nested classes by domain.

```python
class Settings(BaseSettings):
    api: ApiSettings         # Host, port, CORS, debug, rate limiting
    db: DatabaseSettings     # Connection URL, pool size
    celery: CelerySettings   # Broker URL, result backend
    minio: MinIOSettings     # Endpoint, credentials, bucket names
    ocr: OCRSettings         # DPI, language, thresholds
    template: TemplateSettings  # pHash threshold, ORB params, match score
    confidence: ConfidenceSettings  # Auto-finalize, needs-review thresholds
    vlm: VlmSettings         # LayoutLM model name, multiplier, timeout
    hitl: HitlSettings       # Enabled, thresholds, min flags
    features: FeatureFlags   # ENABLE_VLM, ENABLE_VLM, etc.
```

Each sub-class reads from environment variables with its own prefix. For example, `OCRSettings` reads `OCR_ENABLE_GPU`, `OCR_DPI_TARGET`, etc.

The `settings` singleton is instantiated once at module import time and shared across the process. Import it with:
```python
from libs.shared.config.settings import settings
```

---

## Database Access Pattern

The project uses the SQLAlchemy 2.0 ORM with synchronous sessions (not async) because PaddleOCR and the Celery worker are synchronous. AsyncIO would add complexity without benefit here.

**Session management**: Each API request gets its own session via FastAPI dependency injection:
```python
def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
        session.commit()
```

**Repository pattern**: All database access goes through repository classes in `libs/shared/db/repositories/`. Routes and worker tasks call repositories, not SQLAlchemy directly. This keeps query logic centralized and testable.

**Enum types**: PostgreSQL ENUM types are defined in migrations. The Python enum classes in `libs/shared/db/models/enums.py` must match the database enum values exactly. When adding new enum values, run an `ALTER TYPE ... ADD VALUE IF NOT EXISTS` migration as a separate transaction (PostgreSQL requires this outside a transaction block).

---

## Perceptual Hashing

The template matching system uses difference hashing (dHash), implemented in `libs/shared/template/phash.py`.

**Algorithm**:
1. Resize the page image to 9×8 pixels (gray)
2. For each of the 64 pixel pairs (horizontally adjacent), set the corresponding bit to 1 if the left pixel is brighter than the right pixel
3. The resulting 64-bit integer is the hash

Two images are considered visually similar if their Hamming distance (number of differing bits) is ≤ 10. The threshold is configurable via `TEMPLATE_PHASH_THRESHOLD`.

**Sign handling**: PostgreSQL `bigint` is signed (–2^63 to 2^63–1). Python hashes may be in the unsigned range (0 to 2^64–1). The `PerceptualHasher.to_signed()` and `to_unsigned()` methods convert between representations. The seed script and the pipeline both use `to_signed()` before writing to the database.

**DPI sensitivity**: pHash is computed from an image resized to 9×8, so large DPI differences between the template sample and the incoming document will produce different hashes. All template samples must be rendered at 300 DPI, and the pipeline must process incoming documents at 300 DPI. This is enforced by `OCR_DPI_TARGET=300` in the configuration.

---

## OCR Architecture

PaddleOCR (PP-OCRv5) is a three-stage OCR pipeline:
1. **Text detection**: Detects text region bounding boxes (DB algorithm)
2. **Text direction classification**: Determines if text is right-side-up or rotated
3. **Text recognition**: Reads the text in each detected region (CRNN/CTC)

**Subprocess isolation**: PaddleOCR uses Paddle's C++ runtime which, if initialized in the same process as SQLAlchemy and its C extensions, can cause memory corruption on Windows. The solution is to run OCR in a child subprocess via Python's `subprocess` module. The worker spawns a subprocess, passes the image path, and reads the JSON token output back over stdout. This adds ~1–2 seconds of process startup overhead but eliminates the reliability issue.

**Output normalization**: Token bounding boxes from PaddleOCR are in pixel coordinates. The pipeline normalizes them to 0.0–1.0 relative to the page dimensions before storing. LayoutLM, however, expects 0–1000 integer coordinates — the LayoutLM extractor multiplies normalized values by 1000 when building its input.

---

## LayoutLM Integration

**Model**: `impira/layoutlm-document-qa` (Apache 2.0 license, ~130 MB download)

LayoutLM is a transformer that takes a document image, OCR tokens, and bounding boxes as input, plus a natural language question, and outputs the answer as a text span extracted from the document. Because answers are real document spans (extractive, not generative), hallucination is not a concern.

**Question variants**: Three question phrasings are used per field (`FIELD_QUESTIONS_VARIANTS` in `layoutlm_client.py`). Different phrasings activate different attention patterns, improving recall on fields that appear in unexpected locations. The top-scoring answer across all variants is used.

**Singleton loading**: LayoutLM loads in 30–120 seconds on first use. The worker loads it once on startup and keeps it in memory for the process lifetime. The `_layoutlm_client` singleton in `process_fax.py` prevents reload between jobs.

**LoRA fine-tuning**: The model can be fine-tuned using a LoRA (Low-Rank Adaptation) adapter without modifying the base model weights. The adapter adds a small set of trainable parameters (~1 MB) on top of the frozen base model. The adapter file is stored in MinIO and loaded via `peft.PeftModel.from_pretrained()`. If no adapter is configured, the base model is used.

**Score multiplier**: LayoutLM confidence scores are scaled by `VlmSettings.layoutlm_score_multiplier` before merging with template and OCR label candidates. The default is 0.70 for the base model (conservative — template extraction is generally more reliable) and 1.20 for a fine-tuned adapter.

---

## Label-Anchored Template Extraction

The primary extraction method is described here in more detail.

**Core idea**: Instead of using fixed pixel ROIs (which break when documents are printed slightly off-center), find the field label in the OCR token list and extract the value relative to the label's position. This is far more robust to layout variations.

**Extraction flow** in `template_extractor.py`:

```
1. Find label tokens:
   - Search all OCR tokens for text matching label_search_text
   - If label_search_region is defined, restrict search to that area
   - Score label match quality (exact vs. fuzzy)

2. Collect inline value:
   - Tokens to the right of the label, same approximate y-position
   - Stop at significant horizontal gap or right edge of ROI

3. If no inline value, collect below value:
   - Tokens directly below the label, aligned to label's left edge
   - Apply column alignment: col_x1 = label_x1 + 0.06

4. Apply field-type trimming (_trim_value_by_type):
   - auth_number: strip "PA-", "REF:", "AUTH#:", etc.
   - date: if range ("08/01 – 09/01"), take the first date
   - service_code: per-word check, reject words < 4 chars or non-alphanumeric
   - phone/fax: reject unless matches phone pattern
   - npi: reject unless exactly 10 digits

5. Validate and score:
   - Validation bonus: +0.10 if field type validation passes
   - No fallback for strict types: if validation fails, return empty (not garbage)
```

---

## Multi-Source Field Builder

`libs/shared/extraction/field_builder.py` merges candidates from all three extraction sources. The key scoring logic:

**Base scores by method:**
- `TEMPLATE_OCR`: Score from template extractor (0.0–1.0)
- OCR label: Regex match score (0.0–1.0) with +0.10 validation bonus
- `LAYOUTLM`: Model confidence × `layoutlm_score_multiplier`

**Scoring adjustments:**
- `HYBRID` bonus: +0.05 if two or more sources produced the same value (case-insensitive, stripped)
- Contamination penalty: –0.20 if the candidate value appears in a different field's known-good value set (prevents field bleed)
- Validation penalty: –0.30 if the field-type validation fails

**NOT_PRESENT**: If the winning candidate's final score is < 0.30, the field is classified as not present (`not_present = True`, `value = None`, `confidence = 0.0`).

---

## HITL System

`libs/shared/extraction/hitl.py` contains two pure functions:

**`compute_field_flags(extraction_json, ...)`**:
- Iterates over all fields in the extraction result
- Skips fields with `method = HUMAN_REVIEW` (already authoritative)
- Applies critical/default threshold based on whether the field is in the critical set
- Returns a list of flag dicts, sorted critical-first

**`apply_human_corrections(extraction_json, corrections)`**:
- Does not mutate the input dict (returns a new dict)
- For each corrected field:
  - Moves the original machine value to `candidates[]` with `_superseded_by_human = True`
  - Sets `value` to the correction, `confidence = 1.0`, `method = HUMAN_REVIEW`
- Corrected field keys are removed from `flagged_fields` in the DB by `extraction_repo.apply_corrections()`

---

## LayoutLM Fine-tuning Workflow

**Training data generation** (`scripts/generate_training_data.py`):

Source A — seeded templates:
- Loads field definitions from `fax_template_field`
- Renders content pages from `pdfs/Templetes_pdf/` at 300 DPI
- Creates `FaxLabelExample` records with `source = "synthetic_template"`

Source B — verified batch jobs:
- Queries `fax_job` with status COMPLETED or NEEDS_REVIEW and known payer
- Uses existing extraction results as ground truth labels
- Creates `FaxLabelExample` records with `source = "human_reviewed"`

**Fine-tuning** (`scripts/finetune_layoutlm.py`):

Key design decisions:
- **Offset-mapping span match**: Uses tokenizer's `return_offsets_mapping=True` to find answer character offsets rather than fragile token-ID comparison. Examples where the answer span cannot be aligned are skipped.
- **Three question variants per field**: Each labeled example generates three training QA pairs (one per question phrasing in `FIELD_QUESTIONS_VARIANTS`).
- **Stratified oversampling**: Fields with fewer training examples are oversampled to match the most-represented field, preventing the model from ignoring rare fields.
- **GT validation**: Ground truth values are validated against field-type rules before being added to the training set. Invalid ground truth (e.g., a malformed date) is skipped with a warning.

Output: a LoRA adapter directory (saved to disk). The path is printed on completion and should be registered via `POST /v1/models`.

**Scheduled retraining** (`workers/fax_processing_worker/tasks/retrain_layoutlm.py`):
- Runs weekly via Celery Beat
- Checks count of new `fax_label_example` records since last training run
- If count >= `RETRAIN_MIN_NEW_LABELS` (default 20), triggers fine-tuning
- Registers the new adapter version
- Optionally auto-promotes if `RETRAIN_AUTO_PROMOTE=true`

---

## Security Implementation Details

**JWT**: Tokens are signed with HS256 using `SECRET_KEY`. The `get_current_user` dependency in `libs/shared/utils/auth.py` decodes and validates the token on every request. The user object carries `tenant_id` and `is_admin` claims.

**Tenant isolation**: All repository methods that list or get records accept a `tenant_id` parameter. The repository joins `fax_job` on `tenant_id` at the query level. Route handlers pass `user.tenant_id` for non-admin users and `None` for admin users (`None` = no tenant filter = see all).

**Rate limiting** (`libs/shared/utils/rate_limiter.py`): Token-bucket algorithm. Each user has a bucket with a capacity and a refill rate. A background daemon thread (not a Celery task) runs every 5 minutes to evict buckets for users who have been inactive. The limiter is instantiated as a module-level singleton in each API's `main.py`.

**Audit logging**: The `AuditLogger` in `libs/shared/utils/auth.py` writes to the `audit_log` table. It is called explicitly in route handlers that access PHI — not as middleware — because the PHI scope varies by route. The `claim_review` route writes the audit log before `db.commit()` to ensure the log is always written even if the commit fails.

**Credential enforcement**: `libs/shared/config/settings.py` checks `ENVIRONMENT == "production"` and raises `RuntimeError` at startup if any of `SECRET_KEY`, `MINIO_ACCESS_KEY`, or `MINIO_SECRET_KEY` are the default development values.

---

## Adding a New Payer

1. **Obtain a sample PDF** in the format the payer sends (ideally a blank template form).

2. **Add the payer to the enum** in `libs/shared/db/models/enums.py`:
   ```python
   class PayerNameEnum(str, enum.Enum):
       NEW_PAYER = "NEW_PAYER"
   ```

3. **Run a migration** to add the enum value to PostgreSQL:
   ```bash
   docker exec fax_postgres psql -U faxadmin -d fax_processor \
     -c "ALTER TYPE payer_name_enum ADD VALUE IF NOT EXISTS 'NEW_PAYER';"
   ```

4. **Add keyword rules** to `configs/payer_rules.yml` for OCR-based payer detection fallback.

5. **Seed the template**:
   - Add the PDF to `pdfs/Templetes_pdf/`
   - Add the payer entry to `scripts/seed_templates.py` following the existing pattern
   - Run `python scripts/seed_templates.py`

6. **Verify extraction**: Upload a sample document and confirm payer detection and field extraction work correctly.

---

## Running Tests

```bash
# All unit tests
pytest tests/unit/ -v

# Specific test file
pytest tests/unit/test_hitl.py -v

# With coverage
pytest tests/unit/ --cov=libs --cov=services --cov=workers \
  --cov-report=term-missing --cov-report=html

# Fast run (no slow model tests)
pytest tests/unit/ -v -m "not slow"
```

The test suite does not require running Docker or any external services. All database and model interactions are mocked. The 29 LayoutLM tests use a mock pipeline that returns pre-defined answers, so they do not require the model to be downloaded.

**Test structure:**
```
tests/unit/
  test_template_extractor.py   Label-anchored extraction logic
  test_ocr_label_extractor.py  Regex label matching
  test_field_builder.py        Multi-source merging and scoring
  test_phash.py                pHash computation and Hamming distance
  test_hitl.py                 HITL flagging and correction application
  test_output_formatter.py     Client-facing format conversion
  test_layoutlm.py             LayoutLM client (mocked model)
  test_confidence.py           Confidence scoring
  ...
```

---

## Common Development Tasks

### Apply a migration manually
```bash
docker exec -i fax_postgres psql -U faxadmin -d fax_processor \
  < infra/migrations/009_hitl_flagged_fields.sql
```

### Inspect the database
```bash
docker exec -it fax_postgres psql -U faxadmin -d fax_processor
```

Useful queries:
```sql
-- Recent jobs
SELECT fax_job_id, status, payer_name, created_at FROM fax_job ORDER BY created_at DESC LIMIT 10;

-- Extraction result for a job
SELECT extraction_json FROM fax_extraction WHERE fax_job_id = '<uuid>';

-- Flagged fields
SELECT fax_job_id, flagged_fields FROM fax_extraction WHERE flagged_fields != '[]';

-- Audit log
SELECT action, user_id, resource_id, created_at FROM audit_log ORDER BY created_at DESC LIMIT 20;
```

### View worker logs
```bash
docker compose logs -f fax_worker
```

### Force re-seed templates (clears existing and re-inserts)
```bash
docker compose exec fax-ingress python /app/scripts/seed_templates.py --force
```

### Generate training data and inspect
```bash
docker compose exec fax-ingress python /app/scripts/generate_training_data.py --source all --dry-run
```

### Check which model version is active
```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8002/v1/models/active
```

---

## Known Limitations

| Limitation | Detail |
|---|---|
| Buckeye narrative format | Buckeye sends documents in paragraph form, not structured fields. Only `auth_number` and `patient_name` are reliably extractable. |
| PaddleOCR non-determinism | PaddleOCR may produce slightly different token groupings between runs on the same image, particularly for closely-spaced text. This can cause minor confidence variation on re-runs. |
| LayoutLM date confusion | The base LayoutLM model sometimes returns the same date for multiple date fields. Fine-tuning on healthcare fax data significantly reduces this. |
| First-run model download time | LayoutLM and PaddleOCR models download on first use (~130 MB and ~200 MB respectively). In Docker, these are cached in named volumes so subsequent starts are fast. |
| CPU-only processing | The default configuration runs all models on CPU. Processing a typical 3-page fax takes 30–90 seconds. GPU support is available but requires a CUDA-equipped host and different base Docker image. |
| Single-worker sequential processing | One Celery task = one fax at a time per worker process. Scale horizontally with additional worker containers. |

---

## Environment Variable Quick Reference

All variables read by the application, grouped by component:

### API
`API_HOST`, `API_DEBUG`, `API_LOG_LEVEL`, `API_MAX_UPLOAD_SIZE_MB`, `API_ALLOWED_ORIGINS`, `API_ALLOWED_HOSTS`

### Database
`DATABASE_URL`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`

### Redis / Celery
`REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`

### MinIO
`MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_SECURE`, `MINIO_BUCKET_NAME`, `MINIO_TEMPLATES_BUCKET`

### OCR
`OCR_ENABLE_GPU`, `OCR_DPI_TARGET`, `OCR_LANG`, `OCR_USE_ANGLE_CLS`, `OCR_DET_DB_THRESH`, `OCR_DET_DB_BOX_THRESH`, `OCR_REC_BATCH_NUM`

### Template Matching
`TEMPLATE_PHASH_THRESHOLD`, `TEMPLATE_ORB_MIN_MATCHES`, `TEMPLATE_MATCH_MIN_SCORE`, `TEMPLATE_ORB_N_FEATURES`

### VLM / LayoutLM
`VLM_LAYOUTLM_MODEL_NAME`, `VLM_LAYOUTLM_ENABLED`, `VLM_LAYOUTLM_ADAPTER_PATH`, `VLM_LAYOUTLM_SCORE_MULTIPLIER`

### Confidence
`CONFIDENCE_AUTO_FINALIZE`, `CONFIDENCE_NEEDS_REVIEW`, `CONFIDENCE_FIELD_MIN`, `CONFIDENCE_CRITICAL_FIELD_MIN`

### HITL
`HITL_ENABLED`, `HITL_DEFAULT_THRESHOLD`, `HITL_CRITICAL_THRESHOLD`, `HITL_MIN_FLAGS_FOR_REVIEW`

### Feature Flags
`ENABLE_VLM`, `ENABLE_VLM`, `ENABLE_VECTOR_SEARCH`, `ENABLE_FEEDBACK_LOOP`

### Security
`SECRET_KEY`, `ENVIRONMENT`

### Retraining
`RETRAIN_MIN_NEW_LABELS`, `RETRAIN_AUTO_PROMOTE`

### Model Cache
`HF_HOME` (HuggingFace cache dir), `PADDLEOCR_HOME` (PaddleOCR model cache dir)
