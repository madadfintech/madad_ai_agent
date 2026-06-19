# Quick status check for the Madad Monitor UI daemons.
#
# Usage:  .\monitor_ui\status.ps1

$ErrorActionPreference = "Continue"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runDir = Join-Path $root ".run"

function Get-DaemonStatus([string]$name, [string]$port) {
    $pidFile = Join-Path $runDir "$name.pid"
    if (-not (Test-Path $pidFile)) {
        return @{ Name = $name; State = "stopped"; PID = "-"; Port = $port }
    }
    $procId = (Get-Content $pidFile).Trim()
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($null -eq $proc) {
        return @{ Name = $name; State = "stale-pid"; PID = $procId; Port = $port }
    }
    return @{ Name = $name; State = "running"; PID = $procId; Port = $port }
}

$rows = @(
    Get-DaemonStatus "backend"  "5001"
    Get-DaemonStatus "frontend" "5173"
)

Write-Host ""
Write-Host "Madad Monitor UI" -ForegroundColor Cyan
Write-Host "-----------------"
foreach ($r in $rows) {
    $color = switch ($r.State) {
        "running"   { "Green" }
        "stopped"   { "DarkGray" }
        "stale-pid" { "Yellow" }
    }
    $stateLabel = switch ($r.State) {
        "running"   { "running" }
        "stopped"   { "stopped" }
        "stale-pid" { "stale (pid file points at a dead process)" }
    }
    Write-Host ("  {0,-9} {1,-12} pid={2,-8} port={3}" -f $r.Name, $stateLabel, $r.PID, $r.Port) -ForegroundColor $color
}
Write-Host ""

# If anything's running, probe the backend's /api/connection for monitor health.
if ($rows[0].State -eq "running") {
    try {
        $health = Invoke-RestMethod -TimeoutSec 3 "http://127.0.0.1:5001/api/connection"
        $tunnel = if ($health.tunnel.open) { "open" } else { "down" }
        $monitor = if ($health.ok) { "ok" } else { "unreachable" }
        Write-Host ("  tunnel    : {0}" -f $tunnel)
        Write-Host ("  monitor   : {0}" -f $monitor)
        Write-Host ""
    } catch {
        Write-Host "  (backend not yet responsive - give it ~2s after start)" -ForegroundColor DarkGray
        Write-Host ""
    }
}
