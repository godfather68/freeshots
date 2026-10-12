<#
.SYNOPSIS
  Show the state of the Shots tasks, the web app, the worker (lock, last runs, what is due) and recent logs.
  Read-only: changes nothing.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\status.ps1
#>
param([int]$Port = 8000, [int]$LogLines = 10)
$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "common.ps1")

Write-Host "== Scheduled tasks"
foreach ($name in @($WebTaskName, $WorkerTaskName, $LegacyTaskName)) {
    $task = Get-ShotsTask $name
    if (-not $task) {
        if ($name -ne $LegacyTaskName) { Write-Host ("{0,-14} not installed (run deploy\install.ps1)" -f $name) }
        continue
    }
    $info = Get-ScheduledTaskInfo -TaskName $name -TaskPath $TaskPath
    $note = if ($name -eq $LegacyTaskName) { "  (DEPRECATED: remove with install.ps1 -RemoveLegacyTask)" } else { "" }
    Write-Host ("{0,-14} state={1}; last run={2}; last result={3}; next run={4}{5}" -f $name, $task.State,
        $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime, $note)
}

Write-Host ""
Write-Host "== Web app (http://127.0.0.1:$Port)"
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 5
    Write-Host "responding: HTTP $($r.StatusCode)"
} catch {
    Write-Host "not responding: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "== Worker"
if (Test-Path -LiteralPath $ShotsPython) {
    Push-Location -LiteralPath $ShotsRoot
    & $ShotsPython -m shots.worker status 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -notmatch "alembic.runtime" }
    Pop-Location
} else {
    Write-Host "python venv not found at $ShotsPython"
}

Write-Host ""
Write-Host "== Recent logs ($ShotsLogDir)"
foreach ($kind in @("web", "worker")) {
    $last = Get-ChildItem -LiteralPath $ShotsLogDir -Filter "${kind}_*.log" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime | Select-Object -Last 1
    if ($last) {
        Write-Host "-- $($last.Name)"
        Get-Content -LiteralPath $last.FullName -Tail $LogLines
    } else {
        Write-Host "-- no $kind log yet"
    }
}
