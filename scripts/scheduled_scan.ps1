# DEPRECATED (2026-10-09): replaced by the "Shots Worker" task from deploy\install.ps1, which runs
# `python -m shots.worker run-once` at logon/boot and every 3 hours (overlap lock, skip-if-fresh, odds
# days handled as due work). Do not register this script any more; if a "Shots scan" task exists,
# remove it with `deploy\install.ps1 -RemoveLegacyTask`. Kept only for reference / manual one-off runs.
#
# Scheduled run: refresh results and odds, save picks, rebuild the UI board and the track record.
# Was registered in Windows Task Scheduler as "Shots scan" (Tuesdays and Fridays, 09:00, when
# football-data.co.uk publishes fixtures with odds).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$logDir = Join-Path $root "reports\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("update_{0:yyyy-MM-dd_HHmm}.log" -f (Get-Date))
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -m shots update *>> $log
exit $LASTEXITCODE
