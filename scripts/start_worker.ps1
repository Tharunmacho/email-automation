# Start the local background stack: Redis, then a Celery worker.
#
# Without this the app still runs — every sync falls back to processing inline
# in the API process, which is what "No ingestion worker is running" (HTTP 503
# from /ingest/poll/async) means. With it, a poll hands each email to the worker
# and returns immediately, the per-message claim lock actually holds, and the
# real-time pop-ups reach the browser from whichever process produced them.
#
#   .\scripts\start_worker.ps1
#
# Two Windows-specific details are baked in on purpose, because getting either
# wrong looks like a broken app rather than a wrong flag:
#
#   * --pool=threads. Celery's default prefork pool does not work on Windows;
#     the worker starts, accepts a task and then dies. The work here is
#     I/O-bound (Gmail, OCR, the LLM), so threads cost almost nothing.
#   * Redis comes from docker-compose. The container is `restart: unless-stopped`,
#     so once Docker Desktop is running it comes back on its own and this script
#     only has to nudge it.

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectDir

$Concurrency = 4

Write-Host "== Resume ingestion worker ==" -ForegroundColor Green

# ---- 1. Redis ------------------------------------------------------------- #
Write-Host "Checking Docker..." -ForegroundColor Cyan
try { docker info --format "{{.ServerVersion}}" | Out-Null } catch {
    Write-Warning "Docker is not running. Start Docker Desktop, then re-run this script."
    Write-Warning "Until then syncs still work — they just run inline in the API process."
    exit 1
}

Write-Host "Starting Redis..." -ForegroundColor Cyan
docker compose up -d redis

# The container reports 'Started' before the server is accepting commands, and a
# worker that connects a moment too early exits instead of retrying.
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    $pong = docker exec resume_redis redis-cli ping 2>$null
    if ($pong -match "PONG") { break }
    Start-Sleep -Milliseconds 500
}
if ($pong -notmatch "PONG") {
    Write-Warning "Redis did not answer within 30s. Check: docker logs resume_redis"
    exit 1
}
Write-Host "Redis is up on 127.0.0.1:6379" -ForegroundColor Green

# ---- 2. Worker ------------------------------------------------------------ #
# Foreground on purpose: this window *is* the worker, so its log is visible and
# Ctrl+C stops it. A backgrounded worker whose log nobody reads is how a failing
# task turns into "the sync silently does nothing".
Write-Host "Starting Celery worker (threads pool, concurrency=$Concurrency)..." -ForegroundColor Cyan
Write-Host "Leave this window open. Ctrl+C to stop." -ForegroundColor DarkGray
python -m celery -A app.tasks.celery_app worker --loglevel=INFO --pool=threads --concurrency=$Concurrency
