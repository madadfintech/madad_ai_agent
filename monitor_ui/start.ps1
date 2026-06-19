# Start the Madad Monitor UI (backend + frontend) as detached background
# processes. Returns control to your shell immediately; use stop.ps1 to
# stop. Logs are written under monitor_ui\.run\*.log.
#
# Usage:  .\monitor_ui\start.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runDir = Join-Path $root ".run"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

function Test-Daemon([string]$name) {
    $pidFile = Join-Path $runDir "$name.pid"
    if (-not (Test-Path $pidFile)) { return $false }
    $procId = (Get-Content $pidFile).Trim()
    if (-not $procId) { return $false }
    return $null -ne (Get-Process -Id $procId -ErrorAction SilentlyContinue)
}

if ((Test-Daemon "backend") -or (Test-Daemon "frontend")) {
    Write-Host "Madad Monitor UI is already running." -ForegroundColor Yellow
    Write-Host "Run .\monitor_ui\stop.ps1 first, or .\monitor_ui\status.ps1 to check." -ForegroundColor DarkGray
    exit 1
}

# 1) Backend deps (one-time per machine).
if (-not (Test-Path "$root\backend\.venv")) {
    Write-Host "Creating backend venv..." -ForegroundColor Cyan
    python -m venv "$root\backend\.venv"
}
Write-Host "Installing backend deps..." -ForegroundColor DarkGray
& "$root\backend\.venv\Scripts\python.exe" -m pip install -q -r "$root\backend\requirements.txt"

# 2) Frontend deps (one-time per machine).
if (-not (Test-Path "$root\frontend\node_modules")) {
    Write-Host "Installing frontend deps..." -ForegroundColor Cyan
    Push-Location "$root\frontend"
    npm install --silent --no-audit --no-fund
    Pop-Location
}

# 3) Launch detached. Use Start-Process so each child gets a fresh
# console and inherits no handle from this shell - Ctrl-C here won't
# kill them; stop.ps1 will.
$backendLog  = Join-Path $runDir "backend.log"
$frontendLog = Join-Path $runDir "frontend.log"

# Backend - uvicorn from monitor_ui/ so `backend.main:app` resolves to
# monitor_ui/backend/main.py.
$backend = Start-Process `
    -FilePath "$root\backend\.venv\Scripts\python.exe" `
    -ArgumentList "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "5001" `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -PassThru `
    -RedirectStandardOutput $backendLog `
    -RedirectStandardError "$runDir\backend.err"

$backend.Id | Out-File -FilePath (Join-Path $runDir "backend.pid") -Encoding ascii

# Frontend - npm run dev. npm.cmd lives next to node on PATH.
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
    Write-Host "npm not found on PATH." -ForegroundColor Red
    Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

$frontend = Start-Process `
    -FilePath "cmd.exe" `
    -ArgumentList "/c", "npm run dev" `
    -WorkingDirectory "$root\frontend" `
    -WindowStyle Hidden `
    -PassThru `
    -RedirectStandardOutput $frontendLog `
    -RedirectStandardError "$runDir\frontend.err"

$frontend.Id | Out-File -FilePath (Join-Path $runDir "frontend.pid") -Encoding ascii

Start-Sleep -Seconds 2

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host "  Madad Monitor UI - started"
Write-Host "  Backend  : http://127.0.0.1:5001  (PID $($backend.Id))"
Write-Host "  Frontend : http://localhost:5173  (PID $($frontend.Id))"
Write-Host "  Logs     : $runDir"
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Tail logs:" -ForegroundColor DarkGray
Write-Host "  Get-Content -Wait $backendLog"
Write-Host "  Get-Content -Wait $frontendLog"
Write-Host ""
Write-Host "Stop:    .\monitor_ui\stop.ps1" -ForegroundColor DarkGray
Write-Host "Status:  .\monitor_ui\status.ps1" -ForegroundColor DarkGray
