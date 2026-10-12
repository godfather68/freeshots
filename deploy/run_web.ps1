<#
.SYNOPSIS
  Task action of "Shots Web": serve the web app on 127.0.0.1 and restart it if it stops.

.DESCRIPTION
  Runs python -m shots.web --host 127.0.0.1 --port <Port>, writing its output to
  reports\logs\web_<date>.log (a new file each day; files older than -KeepLogDays are deleted).
  If the server exits it is restarted after a delay that doubles up to 5 minutes (reset after a
  run of 10 minutes or more), so a busy port or a broken build does not spin.
  The server's access log is off by default (it would record client IPs and URLs); set
  SHOTS_ACCESS_LOG=1 in the environment to enable it. Proxy headers are off (SHOTS_PROXY_HEADERS).
#>
param(
    [int]$Port = 8000,
    [int]$KeepLogDays = 14
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location -LiteralPath $ShotsRoot
$delay = 5
while ($true) {
    Remove-ShotsOldLogs -KeepDays $KeepLogDays
    Write-ShotsLog "web" "start: python -m shots.web --host 127.0.0.1 --port $Port"
    $started = Get-Date
    $ErrorActionPreference = "Continue"  # the server logs to stderr; those lines are not errors here
    & $ShotsPython -m shots.web --host 127.0.0.1 --port $Port 2>&1 | ForEach-Object {
        Add-Content -LiteralPath (Get-ShotsDailyLog "web") -Value "$_" -Encoding UTF8
    }
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if (((Get-Date) - $started).TotalMinutes -ge 10) { $delay = 5 }
    Write-ShotsLog "web" "server exited with code $code; restarting in $delay s"
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, 300)
}
