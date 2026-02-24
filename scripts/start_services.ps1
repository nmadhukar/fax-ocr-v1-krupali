# Start API + Worker for production-level testing
$ROOT = "c:\Users\suran\Desktop\fax_ocr\fax_ocr"
$PYTHON = "$ROOT\venv\Scripts\python.exe"

$env:PYTHONPATH = $ROOT
$env:DATABASE_URL = "postgresql+psycopg2://faxadmin:faxpass123@127.0.0.1:5432/fax_processor"
$env:REDIS_URL = "redis://127.0.0.1:6379/0"
$env:CELERY_BROKER_URL = "redis://127.0.0.1:6379/0"
$env:CELERY_RESULT_BACKEND = "redis://127.0.0.1:6379/1"
$env:MINIO_ENDPOINT = "127.0.0.1:9000"
$env:MINIO_ACCESS_KEY = "minioadmin"
$env:MINIO_SECRET_KEY = "minioadmin123"
$env:ENVIRONMENT = "development"
$env:SECRET_KEY = "dev-secret-key-batch3-test"
$env:ENABLE_VLM = "true"
$env:VLM_LAYOUTLM_ENABLED = "true"

Write-Host "Starting Ingress API on :8001..."
$api = Start-Process -FilePath $PYTHON `
    -ArgumentList "-m","uvicorn","services.fax_ingress_api.main:app","--host","0.0.0.0","--port","8001","--log-level","warning" `
    -WorkingDirectory $ROOT -WindowStyle Hidden -PassThru
Write-Host "API PID: $($api.Id)"

Start-Sleep -Seconds 4

Write-Host "Starting Celery Worker..."
$worker = Start-Process -FilePath $PYTHON `
    -ArgumentList "-m","celery","-A","workers.fax_processing_worker.celery_app","worker","--loglevel=warning","--pool=solo" `
    -WorkingDirectory $ROOT -WindowStyle Hidden -PassThru
Write-Host "Worker PID: $($worker.Id)"

Write-Host "Services started: API=$($api.Id) Worker=$($worker.Id)"
"$($api.Id),$($worker.Id)" | Out-File "$ROOT\scripts\pids.txt"
