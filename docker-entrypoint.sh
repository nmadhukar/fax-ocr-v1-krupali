#!/bin/bash
set -e

# ============================================================================
# Healthcare Fax OCR — Docker Entrypoint
# Waits for dependencies, runs migrations/seed if flagged, then starts service
# ============================================================================

echo "=== Healthcare Fax OCR Pipeline — Starting ==="
echo "Service command: $@"

# ---- Parse DB connection from DATABASE_URL ----
# Format: postgresql+psycopg2://user:pass@host:port/dbname
DB_HOST=$(echo "$DATABASE_URL" | sed -E 's|.*@([^:]+):([0-9]+)/.*|\1|')
DB_PORT=$(echo "$DATABASE_URL" | sed -E 's|.*@([^:]+):([0-9]+)/.*|\2|')
DB_USER=$(echo "$DATABASE_URL" | sed -E 's|.*://([^:]+):.*|\1|')
DB_NAME=$(echo "$DATABASE_URL" | sed -E 's|.*/([^?]+).*|\1|')

echo "DB: ${DB_HOST}:${DB_PORT}/${DB_NAME} as ${DB_USER}"

# ---- Wait for PostgreSQL ----
echo "Waiting for PostgreSQL..."
MAX_RETRIES=30
RETRY=0
until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -q 2>/dev/null; do
    RETRY=$((RETRY + 1))
    if [ "$RETRY" -ge "$MAX_RETRIES" ]; then
        echo "ERROR: PostgreSQL not ready after ${MAX_RETRIES} retries"
        exit 1
    fi
    echo "  Postgres not ready (attempt ${RETRY}/${MAX_RETRIES})..."
    sleep 2
done
echo "PostgreSQL is ready!"

# ---- Wait for Redis ----
REDIS_HOST=$(echo "$REDIS_URL" | sed -E 's|redis://([^:]+):.*|\1|')
REDIS_PORT=$(echo "$REDIS_URL" | sed -E 's|redis://[^:]+:([0-9]+).*|\1|')
echo "Waiting for Redis at ${REDIS_HOST}:${REDIS_PORT}..."
RETRY=0
until python -c "import redis; r=redis.Redis(host='${REDIS_HOST}', port=${REDIS_PORT}); r.ping()" 2>/dev/null; do
    RETRY=$((RETRY + 1))
    if [ "$RETRY" -ge "$MAX_RETRIES" ]; then
        echo "ERROR: Redis not ready after ${MAX_RETRIES} retries"
        exit 1
    fi
    echo "  Redis not ready (attempt ${RETRY}/${MAX_RETRIES})..."
    sleep 2
done
echo "Redis is ready!"

# ---- Wait for MinIO ----
echo "Waiting for MinIO at ${MINIO_ENDPOINT}..."
RETRY=0
until curl -sf "http://${MINIO_ENDPOINT}/minio/health/live" > /dev/null 2>&1; do
    RETRY=$((RETRY + 1))
    if [ "$RETRY" -ge "$MAX_RETRIES" ]; then
        echo "WARNING: MinIO not ready after ${MAX_RETRIES} retries (continuing anyway)"
        break
    fi
    echo "  MinIO not ready (attempt ${RETRY}/${MAX_RETRIES})..."
    sleep 2
done
echo "MinIO is ready!"

# ---- Run Migrations (only if flagged) ----
if [ "${RUN_MIGRATIONS}" = "true" ]; then
    echo ""
    echo "=== Running Database Migrations ==="

    DB_PASS=$(echo "$DATABASE_URL" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')
    export PGPASSWORD="$DB_PASS"

    MIGRATIONS_DIR="/app/infra/migrations"

    for sql_file in $(ls "$MIGRATIONS_DIR"/*.sql | sort); do
        filename=$(basename "$sql_file")
        echo "  Running: ${filename}..."

        # 004_week2_enums.sql has ALTER TYPE ... ADD VALUE which cannot
        # run inside a transaction block. Run it without --single-transaction.
        if [ "$filename" = "004_week2_enums.sql" ]; then
            # Run each ALTER TYPE statement separately
            while IFS= read -r line; do
                # Skip comments and empty lines
                if echo "$line" | grep -qE "^(--|$)"; then
                    continue
                fi
                # Only run ALTER TYPE lines
                if echo "$line" | grep -qi "ALTER TYPE"; then
                    psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
                        -c "$line" 2>/dev/null || true
                fi
            done < "$sql_file"
        else
            psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
                -f "$sql_file" 2>/dev/null || true
        fi
    done

    unset PGPASSWORD
    echo "=== Migrations Complete ==="
fi

# ---- Seed Templates (only if flagged) ----
if [ "${RUN_SEED}" = "true" ]; then
    echo ""
    echo "=== Seeding Templates ==="

    # Check if templates already exist
    DB_PASS=$(echo "$DATABASE_URL" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')
    export PGPASSWORD="$DB_PASS"
    TEMPLATE_COUNT=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        -t -c "SELECT COUNT(*) FROM fax_template;" 2>/dev/null | tr -d ' ' || echo "0")
    unset PGPASSWORD

    if [ "$TEMPLATE_COUNT" -gt "0" ]; then
        echo "  Templates already seeded (${TEMPLATE_COUNT} found). Skipping."
    else
        echo "  No templates found. Running seed script..."
        python /app/scripts/seed_templates.py || {
            echo "WARNING: Template seeding failed (non-fatal, templates can be seeded later)"
        }
    fi

    echo "=== Seeding Complete ==="
fi

echo ""
echo "=== Starting Service ==="
exec "$@"
