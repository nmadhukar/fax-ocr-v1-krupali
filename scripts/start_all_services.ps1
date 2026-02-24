# Start all 4 services: Ingress (8001), Review (8002), Query (8003), Worker
$ROOT   = "c:\Users\suran\Desktop\fax_ocr\fax_ocr"
$PYTHON = "$ROOT\venv\Scripts\python.exe"

$env:PYTHONPATH              = $ROOT
$env:DATABASE_URL            = "postgresql+psycopg2://faxadmin:faxpass123@127.0.0.1:5432/fax_processor"
$env:REDIS_URL               = "redis://127.0.0.1:6379/0"
$env:CELERY_BROKER_URL       = "redis://127.0.0.1:6379/0"
$env:CELERY_RESULT_BACKEND   = "redis://127.0.0.1:6379/1"
$env:MINIO_ENDPOINT          = "127.0.0.1:9000"
$env:MINIO_ACCESS_KEY        = "minioadmin"
$env:MINIO_SECRET_KEY        = "minioadmin123"
$env:ENVIRONMENT             = "development"
$env:SECRET_KEY              = "dev-secret-key-batch3-test"
$env:ENABLE_VLM              = "true"
$env:VLM_LAYOUTLM_ENABLED    = "true"

Write-Host "Starting Ingress API on :8001..."
$ingress = Start-Process -FilePath $PYTHON `
    -ArgumentList "-m","uvicorn","services.fax_ingress_api.main:app","--host","0.0.0.0","--port","8001","--log-level","warning" `
    -WorkingDirectory $ROOT -WindowStyle Hidden -PassThru

Write-Host "Starting Review API on :8002..."
$review = Start-Process -FilePath $PYTHON `
    -ArgumentList "-m","uvicorn","services.fax_review_api.main:app","--host","0.0.0.0","--port","8002","--log-level","warning" `
    -WorkingDirectory $ROOT -WindowStyle Hidden -PassThru

Write-Host "Starting Query API on :8003..."
$query = Start-Process -FilePath $PYTHON `
    -ArgumentList "-m","uvicorn","services.fax_query_api.main:app","--host","0.0.0.0","--port","8003","--log-level","warning" `
    -WorkingDirectory $ROOT -WindowStyle Hidden -PassThru

Start-Sleep -Seconds 3

Write-Host "Starting Celery Worker..."
$worker = Start-Process -FilePath $PYTHON `
    -ArgumentList "-m","celery","-A","workers.fax_processing_worker.celery_app","worker","--loglevel=warning","--pool=solo" `
    -WorkingDirectory $ROOT -WindowStyle Hidden -PassThru

$pids = "$($ingress.Id),$($review.Id),$($query.Id),$($worker.Id)"
$pids | Out-File "$ROOT\scripts\pids.txt"
Write-Host "All services started. PIDs: $pids"
Write-Host "Waiting 10s for initialization..."
Start-Sleep -Seconds 10
Write-Host "Ready."
