# Stop the Madad Monitor UI daemons.
#
# Usage:  .\monitor_ui\stop.ps1

$ErrorActionPreference = "Continue"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runDir = Join-Path $root ".run"

function Stop-Daemon([string]$name) {
    $pidFile = Join-Path $runDir "$name.pid"
    if (-not (Test-Path $pidFile)) {
        Write-Host "$name : not running" -ForegroundColor DarkGray
        return
    }
    $procId = (Get-Content $pidFile).Trim()
    if (-not $procId) {
        Remove-Item $pidFile -Force
        Write-Host "$name : pid file empty, removed" -ForegroundColor DarkGray
        return
    }
    # /T kills the entire process tree (npm spawns node/vite as a child;
    # uvicorn may have its own reloader subprocesses too).
    $null = & taskkill /F /T /PID $procId 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "$name : stopped (pid $procId)" -ForegroundColor Green
    } else {
        Write-Host "$name : already gone (pid $procId)" -ForegroundColor DarkGray
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Path $runDir)) {
    Write-Host "Nothing to stop - .run/ does not exist." -ForegroundColor DarkGray
    exit 0
}

Stop-Daemon "frontend"
Stop-Daemon "backend"

# taskkill returns 128 when a target was already gone. We already handled
# that path; reset so the script exits 0 cleanly.
$global:LASTEXITCODE = 0
exit 0
