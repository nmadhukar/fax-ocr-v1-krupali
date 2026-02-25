# ============================================================================
# Healthcare Fax OCR Pipeline — Multi-Service Dockerfile
# Single image serves: ingress API, review API, query API, Celery worker/beat
#
# ALL ML models are baked into this image at build time:
#   • PaddleOCR (English det + rec + cls)   ~100 MB
#   • LayoutLM Document QA                   ~130 MB
#   • sentence-transformers/all-MiniLM-L6-v2  ~90 MB
#   • Swagger UI assets                         ~1 MB
#
# After `docker compose build`, containers start with ZERO internet downloads.
# ============================================================================

# Stage 1: Builder — install Python dependencies
FROM python:3.11-slim-bookworm AS builder

# System deps needed for building native extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    swig \
    libpq-dev \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy requirements and install Python packages
COPY requirements.txt .

# Install CPU-only torch first (saves ~1.5GB vs CUDA)
RUN pip install --no-cache-dir \
    torch==2.1.2 --index-url https://download.pytorch.org/whl/cpu

# Install CPU-only PaddlePaddle
RUN pip install --no-cache-dir \
    paddlepaddle==2.6.2

# Install remaining dependencies (skip torch/paddle since already installed)
RUN grep -v -E "^(torch|paddlepaddle)" requirements.txt > requirements_filtered.txt && \
    pip install --no-cache-dir -r requirements_filtered.txt

# ============================================================================
# Stage 2: Runtime
# ============================================================================
FROM python:3.11-slim-bookworm AS runtime

# Runtime system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    libpq5 \
    poppler-utils \
    curl \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /app

# Environment (set early so model download uses correct cache paths)
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface \
    PADDLEOCR_HOME=/app/.cache/paddleocr \
    TRANSFORMERS_OFFLINE=0

# Create cache directories
RUN mkdir -p /app/.cache/huggingface /app/.cache/paddleocr /app/static/swagger-ui

# ── Pre-download all ML models at build time ───────────────────────────────
# Copy ONLY the download script first so this layer is cached separately
# from application code. Re-runs only if download_models.py changes.
COPY scripts/download_models.py /tmp/download_models.py
RUN python /tmp/download_models.py && rm /tmp/download_models.py

# After models are baked in, switch to offline mode so containers never
# attempt unexpected network calls to Hugging Face at runtime.
ENV TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1

# ── Download Swagger UI assets ─────────────────────────────────────────────
# Served locally — browser does NOT need to reach cdn.jsdelivr.net
RUN curl -fsSL "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.10.3/swagger-ui-bundle.js" \
         -o /app/static/swagger-ui/swagger-ui-bundle.js && \
    curl -fsSL "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.10.3/swagger-ui.css" \
         -o /app/static/swagger-ui/swagger-ui.css

# ── Copy application code ──────────────────────────────────────────────────
COPY libs/ ./libs/
COPY services/ ./services/
COPY workers/ ./workers/
COPY configs/ ./configs/
COPY scripts/ ./scripts/
COPY infra/migrations/ ./infra/migrations/
COPY infra/init-scripts/ ./infra/init-scripts/

# Copy entrypoint
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

# Run as non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser \
    && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["/app/docker-entrypoint.sh"]
