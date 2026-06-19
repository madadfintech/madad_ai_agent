# Local launcher — backend + frontend in two background jobs.
# Run from the repo root: .\monitor_ui\start.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1) Backend dependencies (one-time per machine).
if (-not (Test-Path "$root\backend\.venv")) {
    Write-Host "Creating backend venv..." -ForegroundColor Cyan
    python -m venv "$root\backend\.venv"
}
& "$root\backend\.venv\Scripts\python.exe" -m pip install -q -r "$root\backend\requirements.txt"

# 2) Frontend dependencies (one-time per machine).
if (-not (Test-Path "$root\frontend\node_modules")) {
    Write-Host "Installing frontend deps..." -ForegroundColor Cyan
    Push-Location "$root\frontend"
    npm install --silent
    Pop-Location
}

# 3) Launch both.
Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host "  Madad Monitor UI"
Write-Host "  Backend: http://127.0.0.1:5001"
Write-Host "  Frontend: http://localhost:5173"
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""

$backend = Start-Job -ScriptBlock {
    param($root)
    Set-Location $root
    & ".\backend\.venv\Scripts\python.exe" -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 5001
} -ArgumentList "$root\.."

$frontend = Start-Job -ScriptBlock {
    param($dir)
    Set-Location $dir
    npm run dev
} -ArgumentList "$root\frontend"

Write-Host "Backend job:  $($backend.Id)" -ForegroundColor DarkGray
Write-Host "Frontend job: $($frontend.Id)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Tail logs with: Receive-Job <id> -Wait" -ForegroundColor DarkGray
Write-Host "Stop both with: Stop-Job $($backend.Id),$($frontend.Id); Remove-Job $($backend.Id),$($frontend.Id)" -ForegroundColor DarkGray
Write-Host ""

try {
    Receive-Job -Job $backend, $frontend -Wait
} finally {
    Stop-Job -Job $backend, $frontend -ErrorAction SilentlyContinue
    Remove-Job -Job $backend, $frontend -ErrorAction SilentlyContinue
}
