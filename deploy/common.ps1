# Shared settings and helpers for the deploy scripts (dot-sourced; does nothing on its own).
# Windows PowerShell 5.1 compatible.

$ShotsRoot = Split-Path -Parent $PSScriptRoot
$ShotsPython = Join-Path $ShotsRoot ".venv\Scripts\python.exe"
$ShotsLogDir = Join-Path $ShotsRoot "reports\logs"
$ShotsDeployDir = $PSScriptRoot
$WebTaskName = "Shots Web"
$WorkerTaskName = "Shots Worker"
$LegacyTaskName = "Shots scan"   # registered by hand for the deprecated scripts\scheduled_scan.ps1
$TaskPath = "\"                   # root folder: creating a sub-folder can need admin rights

function Get-ShotsDailyLog([string]$Name) {
    # One file per component and day: reports\logs\<name>_yyyy-MM-dd.log
    return (Join-Path $ShotsLogDir ("{0}_{1:yyyy-MM-dd}.log" -f $Name, (Get-Date)))
}

function Write-ShotsLog([string]$Name, [string]$Message) {
    if (-not (Test-Path -LiteralPath $ShotsLogDir)) { New-Item -ItemType Directory -Force -Path $ShotsLogDir | Out-Null }
    $line = "{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}" -f (Get-Date), $Name, $Message
    Add-Content -LiteralPath (Get-ShotsDailyLog $Name) -Value $line -Encoding UTF8
}

function Remove-ShotsOldLogs([int]$KeepDays) {
    # Rotation: delete web_*/worker_*/update_* logs older than KeepDays (by last write time).
    if ($KeepDays -lt 1 -or -not (Test-Path -LiteralPath $ShotsLogDir)) { return }
    $cut = (Get-Date).AddDays(-$KeepDays)
    Get-ChildItem -LiteralPath $ShotsLogDir -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^(web|worker|update)_.*\.log$' -and $_.LastWriteTime -lt $cut } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

function Test-ShotsElevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-ShotsTask([string]$Name) {
    return Get-ScheduledTask -TaskName $Name -TaskPath $TaskPath -ErrorAction SilentlyContinue
}
