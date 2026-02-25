# Docker Deployment Guide

This guide covers everything needed to run the Healthcare Fax Processing System using Docker Compose. It works identically on Windows, macOS, and Linux.

---

## Prerequisites

| Requirement | Minimum Version | How to Check |
|---|---|---|
| Docker Desktop (Windows/macOS) or Docker Engine (Linux) | 24.0 | `docker --version` |
| Docker Compose | V2 (included with Docker Desktop) | `docker compose version` |
| Available RAM | 6 GB free | Docker Desktop > Settings > Resources |
| Available Disk | 10 GB free | For images, model cache, and data volumes |

**Note on Docker Desktop:** On Windows and macOS, Docker Desktop must be running before any `docker` command will work. On Linux, Docker Engine runs as a background service and does not require a desktop application.

---

## What Gets Deployed

Running `docker compose up` starts eight containers:

| Container | Role | Port |
|---|---|---|
| fax_postgres | PostgreSQL 15 with pgvector | 5432 |
| fax_redis | Redis 7 (task queue and cache) | 6379 |
| fax_minio | MinIO object storage | 9000, 9001 |
| fax_minio_init | One-time bucket creation (exits after setup) | — |
| fax_ingress | Fax upload and query API | 8001 |
| fax_review | Review queue, template admin, analytics | 8002 |
| fax_query | High-performance read API | 8003 |
| fax_worker | Celery processing worker | — |
| fax_beat | Celery beat scheduler (weekly tasks) | — |

The ingress container also runs database migrations and template seeding on first startup. All subsequent starts skip these steps automatically.

---

## Setup: Step by Step

### Step 1 — Get the project files

Copy or clone the project folder to your machine. The folder you need contains:
```
docker-compose.yml
Dockerfile
docker-entrypoint.sh
.env.docker
.env.production
requirements.txt
libs/
services/
workers/
configs/
scripts/
pdfs/
infra/
```

Open a terminal (PowerShell on Windows, Terminal on macOS/Linux) and navigate into the folder:

```bash
cd /path/to/fax_ocr
```

On Windows:
```powershell
cd C:\path\to\fax_ocr
```

### Step 2 — Configure the environment

The file `.env.docker` contains all runtime settings for Docker. The defaults work out of the box for local testing.

For production, copy the production template and fill in real values:

```bash
cp .env.production .env.docker
```

On Windows:
```powershell
Copy-Item .env.production .env.docker
```

At minimum, change these three values in `.env.docker` before a production deployment:

```
SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
MINIO_ACCESS_KEY=<strong username>
MINIO_SECRET_KEY=<strong password>
```

For local testing, the existing `.env.docker` file is ready to use as-is. No changes are needed.

### Step 3 — Build and start

```bash
docker compose up -d
```

This command:
1. Pulls base images (postgres, redis, minio) — approximately 500 MB on first run
2. Builds the application image — approximately 3-4 GB including PyTorch and PaddleOCR
3. Starts all containers in dependency order
4. The ingress container runs migrations and seeds templates before starting the API

**First-time build takes 10-20 minutes** depending on your internet speed and CPU. Subsequent starts (after `docker compose stop`) take under 30 seconds.

Watch the startup progress:

```bash
docker compose logs -f
```

Press Ctrl+C to stop following logs. The containers continue running.

### Step 4 — Verify all services are healthy

```bash
docker compose ps
```

All containers should show `healthy` or `running`. Example output:

```
NAME            STATUS                   PORTS
fax_postgres    running (healthy)        0.0.0.0:5432->5432/tcp
fax_redis       running (healthy)        0.0.0.0:6379->6379/tcp
fax_minio       running (healthy)        0.0.0.0:9000-9001->9000-9001/tcp
fax_minio_init  exited (0)              (completed successfully)
fax_ingress     running (healthy)        0.0.0.0:8001->8001/tcp
fax_review      running (healthy)        0.0.0.0:8002->8002/tcp
fax_query       running (healthy)        0.0.0.0:8003->8003/tcp
fax_worker      running (healthy)
fax_beat        running
```

If any container shows `unhealthy` or `exited (1)`, see the Troubleshooting section.

### Step 5 — Test the API

```bash
# Ingress API
curl http://localhost:8001/health

# Review API
curl http://localhost:8002/health

# Query API
curl http://localhost:8003/health
```

Each should return: `{"status": "ok"}`

---

## Platform-Specific Notes

### Windows

- Use PowerShell or Windows Terminal. Command Prompt (cmd.exe) works for most commands but has limitations with multi-line scripts.
- Docker Desktop must be running (check the system tray).
- If port 5432 is already in use (local PostgreSQL), change the mapping in `docker-compose.yml`: `"5433:5432"`.
- File paths in bind mounts use forward slashes in Docker even on Windows.
- If Docker Desktop shows "WSL 2 backend" in settings, ensure WSL 2 is enabled: `wsl --set-default-version 2`

### macOS

- Docker Desktop must be running (check the menu bar).
- On Apple Silicon (M1/M2/M3), Docker runs via Rosetta 2 emulation for x86 images. All images in this project support `linux/amd64` and `linux/arm64`. Build times may be slightly longer on first run.
- If you see `permission denied` errors on volumes, check Docker Desktop > Settings > Resources > File Sharing and add the project folder.

### Linux

- Install Docker Engine and Docker Compose plugin:
  ```bash
  # Ubuntu/Debian
  sudo apt-get update
  sudo apt-get install docker.io docker-compose-plugin

  # Add your user to the docker group (avoids needing sudo)
  sudo usermod -aG docker $USER
  newgrp docker
  ```
- If ports 5432, 6379, or 9000 are already in use by local services, stop them or change the host port mappings in `docker-compose.yml`.
- SELinux users: add `:z` to volume mounts if containers cannot access mounted files.

---

## API Access After Startup

| Service | URL | Purpose |
|---|---|---|
| Ingress API | http://localhost:8001 | Upload faxes, check status, get results |
| Ingress Swagger | http://localhost:8001/docs | Interactive API documentation |
| Review API | http://localhost:8002 | Review queue, templates, analytics |
| Review Swagger | http://localhost:8002/docs | Interactive API documentation |
| Query API | http://localhost:8003 | High-performance read queries |
| MinIO Console | http://localhost:9001 | File storage browser |

MinIO login: credentials are configured via `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` in `.env.docker`. **These have no default values** and must be explicitly set. The Docker Compose stack provides initial values for local testing — change them before any production deployment.

---

## Uploading a Test Fax

Authentication is required. First get a token (use the default dev credentials from your tenant configuration), then upload:

```bash
# Get auth token
TOKEN=$(curl -s -X POST http://localhost:8001/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Upload a fax PDF
curl -X POST http://localhost:8001/v1/faxes/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/your/fax.pdf" \
  -F "tenant_id=demo_tenant"
```

On Windows PowerShell:
```powershell
# Get auth token
$response = Invoke-RestMethod -Uri "http://localhost:8001/v1/auth/login" `
  -Method POST -ContentType "application/json" `
  -Body '{"username":"admin","password":"admin123"}'
$TOKEN = $response.access_token

# Upload
$form = @{ file = Get-Item "C:\path\to\fax.pdf"; tenant_id = "demo_tenant" }
Invoke-RestMethod -Uri "http://localhost:8001/v1/faxes/upload" `
  -Method POST -Headers @{ Authorization = "Bearer $TOKEN" } `
  -Form $form
```

You can also use the Python helper script (from outside Docker, with Python installed):
```bash
python scripts/do_upload.py --file /path/to/fax.pdf --tenant demo_tenant
```

---

## Monitoring and Logs

View logs from all containers:
```bash
docker compose logs -f
```

View logs from a specific container:
```bash
docker compose logs -f fax_worker
docker compose logs -f fax_ingress
```

View the last 100 lines:
```bash
docker compose logs --tail=100 fax_worker
```

Check container resource usage:
```bash
docker stats
```

---

## Data Persistence

All data is stored in Docker named volumes. Volumes persist across container restarts and even across `docker compose down` calls (unless you use the `-v` flag).

| Volume | Contents |
|---|---|
| postgres_data | Database: all jobs, extractions, templates, audit logs |
| redis_data | Task queue state and results cache |
| minio_data | Uploaded fax PDFs and page images |
| model_cache | Downloaded LayoutLM and HuggingFace model weights |
| paddle_cache | Downloaded PaddleOCR model weights |

The model cache volumes are important — LayoutLM (~130 MB) and PaddleOCR models download on first use. Keeping the volumes means models are not re-downloaded on restart.

---

## Stopping and Restarting

**Stop all containers (keep data):**
```bash
docker compose stop
```

**Start again:**
```bash
docker compose start
```

**Stop and remove containers (keep volumes/data):**
```bash
docker compose down
```

**Full reset — remove everything including all data:**
```bash
docker compose down -v
```
This destroys the database, all uploaded files, and cached models. Use only when you want a completely clean state.

**Restart a single service:**
```bash
docker compose restart fax_worker
```

---

## Rebuilding After Code Changes

If you update the application code, rebuild the image and restart:

```bash
docker compose build
docker compose up -d
```

Or rebuild a single service:
```bash
docker compose build fax-ingress
docker compose up -d fax-ingress
```

---

## Troubleshooting

### Container exits immediately on startup

Check the logs:
```bash
docker compose logs fax_ingress
```

Common causes:
- **Missing `.env.docker` file**: Ensure the file exists in the project root.
- **Database connection refused**: PostgreSQL may still be initializing. Wait 30 seconds and try `docker compose up -d` again.
- **Port already in use**: Another process is using port 8001, 8002, or 8003. Stop it or change the port mapping in `docker-compose.yml`.

### `fax_worker` is unhealthy

The worker performs a Celery ping check. If it fails:
```bash
docker compose logs fax_worker
```
Common cause: Redis not ready, or worker still downloading models on first run. The worker downloads LayoutLM (~130 MB) and PaddleOCR models on first job — this can take several minutes. `start_period: 120s` is set to allow for this, but slow connections may need longer.

### Port conflicts

If any port is occupied, change the host-side port in `docker-compose.yml`. For example, to move the ingress API to port 8011:
```yaml
fax-ingress:
  ports:
    - "8011:8001"    # host:container
```

Then access it at `http://localhost:8011`.

### On Windows: `docker compose` not found

Docker Compose V2 uses `docker compose` (space, not hyphen). Older installations had `docker-compose` (hyphen). Upgrade Docker Desktop to get V2, or install the Compose plugin.

### On Windows: Build fails with path errors

Ensure Docker Desktop is configured to use WSL 2 backend (Settings > General > Use WSL 2 based engine). If using Hyper-V backend, ensure the project drive is shared (Settings > Resources > File Sharing).

### On macOS: `permission denied` on volumes

Go to Docker Desktop > Settings > Resources > File Sharing and add your project directory. Apply and restart Docker Desktop.

### Database migration errors on startup

The entrypoint script runs migrations idempotently — they are safe to run multiple times. If you see migration errors in the logs, the database may have been partially initialized. Run a full reset:
```bash
docker compose down -v
docker compose up -d
```

### MinIO access denied

If the MinIO console shows access errors, ensure the bucket creation ran successfully:
```bash
docker compose logs fax_minio_init
```
You should see `Buckets created successfully`. If not, restart the init container:
```bash
docker compose up fax_minio_init
```

---

## Production Hardening Checklist

Before exposing this system outside a local network:

- [ ] Set `DATABASE_URL` with a strong password (**required** — no default)
- [ ] Set `SECRET_KEY` to a random 32+ character string (**required** — no default)
- [ ] Set `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` to strong values (**required** — no default)
- [ ] Set `ENVIRONMENT=production` in `.env.docker` (triggers credential validation at startup)
- [ ] Set `API_DEBUG=false`
- [ ] Configure `API_ALLOWED_HOSTS` to your actual hostname
- [ ] Configure `API_ALLOWED_ORIGINS` to your frontend domain
- [ ] Place a reverse proxy (nginx, Traefik, Caddy) in front of the APIs
- [ ] Enable TLS on the reverse proxy — never expose the APIs on plain HTTP in production
- [ ] Do not expose ports 5432, 6379, or 9000 publicly — they should be internal only
- [ ] Set `MINIO_SECURE=true` and configure MinIO TLS if MinIO is exposed directly
- [ ] Review and restrict Docker volume permissions on the host
- [ ] Verify tenant isolation is enforced (always on, regardless of environment)
- [ ] Verify file uploads are validated for type, size, and magic bytes

---

## Service Port Reference

| Port | Service | Expose Publicly? |
|---|---|---|
| 8001 | Fax Ingress API | Yes (via reverse proxy) |
| 8002 | Fax Review API | Yes (via reverse proxy) |
| 8003 | Fax Query API | Yes (via reverse proxy) |
| 9001 | MinIO Console | Admin only — restrict by IP |
| 5432 | PostgreSQL | No — internal only |
| 6379 | Redis | No — internal only |
| 9000 | MinIO API | No — internal only |
