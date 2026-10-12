<#
.SYNOPSIS
  Register the two Shots tasks in Windows Task Scheduler for the current user (idempotent).

.DESCRIPTION
  "Shots Web"     at logon (or -AtStartup): deploy\run_web.ps1 -> python -m shots.web on 127.0.0.1,
                  restarted by the wrapper if it stops, no time limit, one instance.
  "Shots Worker"  at logon (or -AtStartup, 2 min delay) and every 3 hours (00:00, 03:00, ... and as
                  soon as possible after a missed slot): deploy\run_worker.ps1 -> python -m shots.worker
                  run-once; 30 min limit; a new instance is not started while one is running.
  Logs: reports\logs\web_<date>.log, worker_<date>.log, rotated after -KeepLogDays days.

  Running again replaces the tasks with the current settings. Nothing is run by this script unless
  -StartNow is given. Use -WhatIf to see what it would do without changing anything.

  Admin rights: NOT needed by default (tasks run as you, at your logon, only while you are logged
  on). -AtStartup makes the tasks start at boot, before anyone logs on (S4U: no password stored);
  Windows requires an elevated PowerShell (Run as administrator) to register boot triggers.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install.ps1 -WhatIf
  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install.ps1 -StartNow
  (elevated) powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install.ps1 -AtStartup -StartNow
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$AtStartup,
    [int]$Port = 8000,
    [int]$KeepLogDays = 14,
    [int]$WorkerEveryHours = 3,
    [switch]$StartNow,
    [switch]$RemoveLegacyTask
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

function Say([string]$m) { Write-Host "[install] $m" }

if (-not (Test-Path -LiteralPath $ShotsPython)) {
    throw "Python venv not found at $ShotsPython. Create it first: python -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt -e ."
}
$user = "$env:USERDOMAIN\$env:USERNAME"
$elevated = Test-ShotsElevated
if ($AtStartup -and -not $elevated -and -not $WhatIfPreference) {
    throw ("-AtStartup needs an elevated PowerShell (right-click PowerShell > Run as administrator): Windows " +
           "only lets administrators register tasks that start at boot. Without admin rights, run this script " +
           "without -AtStartup: the tasks then start when you log on.")
}

Say "project:   $ShotsRoot"
Say "python:    $ShotsPython"
Say "user:      $user ($(if ($AtStartup) { 'at startup, whether logged on or not (S4U)' } else { 'at logon, while logged on' }))"
Say "web:       http://127.0.0.1:$Port (local only)"
Say "worker:    at $(if ($AtStartup) { 'startup' } else { 'logon' }) + every $WorkerEveryHours h; 30 min limit"
Say "logs:      $ShotsLogDir (kept $KeepLogDays days)"

if (-not (Test-Path -LiteralPath $ShotsLogDir)) {
    if ($PSCmdlet.ShouldProcess($ShotsLogDir, "Create log folder")) {
        New-Item -ItemType Directory -Force -Path $ShotsLogDir | Out-Null
    }
}

$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
function New-ShotsAction([string]$Script, [string]$Extra) {
    $arg = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $ShotsDeployDir $Script)`" $Extra"
    return New-ScheduledTaskAction -Execute $ps -Argument $arg.Trim() -WorkingDirectory $ShotsRoot
}

if ($AtStartup) {
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
    $webTrigger = New-ScheduledTaskTrigger -AtStartup
    $workerBoot = New-ScheduledTaskTrigger -AtStartup
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $webTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $workerBoot = New-ScheduledTaskTrigger -AtLogOn -User $user
}
$workerBoot.Delay = "PT2M"  # let the network come up first
# Every N hours from midnight: a daily trigger repeating for one day (works on every Windows 10 build).
$workerRepeat = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today)
$workerRepeat.Repetition = (New-ScheduledTaskTrigger -Once -At ([datetime]::Today) `
    -RepetitionInterval (New-TimeSpan -Hours $WorkerEveryHours) -RepetitionDuration (New-TimeSpan -Days 1)).Repetition

$webSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$workerSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$tasks = @(
    @{ Name = $WebTaskName; Action = (New-ShotsAction "run_web.ps1" "-Port $Port -KeepLogDays $KeepLogDays");
       Triggers = @($webTrigger); Settings = $webSettings;
       Description = "Shots web app on http://127.0.0.1:$Port (restarted if it stops). Installed by deploy\install.ps1." },
    @{ Name = $WorkerTaskName; Action = (New-ShotsAction "run_worker.ps1" "-KeepLogDays $KeepLogDays");
       Triggers = @($workerBoot, $workerRepeat); Settings = $workerSettings;
       Description = "Shots update cycle (python -m shots.worker run-once): at boot/logon and every $WorkerEveryHours h. Installed by deploy\install.ps1." }
)
foreach ($t in $tasks) {
    $verb = if (Get-ShotsTask $t.Name) { "Update" } else { "Register" }
    Say "$verb task '$($t.Name)': $($t.Action.Execute) $($t.Action.Arguments)"
    # built even with -WhatIf (no side effects), so a dry run validates the whole definition
    $task = New-ScheduledTask -Action $t.Action -Trigger $t.Triggers -Settings $t.Settings -Principal $principal `
        -Description $t.Description
    Say ("  triggers: {0}" -f (($task.Triggers | ForEach-Object {
        $r = if ($_.Repetition.Interval) { " every $($_.Repetition.Interval)" } else { "" }
        $d = if ($_.Delay) { " delay $($_.Delay)" } else { "" }
        "$($_.CimClass.CimClassName -replace 'MSFT_Task|Trigger','')$r$d" }) -join ", "))
    Say ("  limit: {0}; instances: {1}" -f $(if ($task.Settings.ExecutionTimeLimit -eq 'PT0S') { 'none' } else { $task.Settings.ExecutionTimeLimit }),
        $task.Settings.MultipleInstances)
    if ($PSCmdlet.ShouldProcess("$TaskPath$($t.Name)", "$verb scheduled task")) {
        Register-ScheduledTask -TaskName $t.Name -TaskPath $TaskPath -InputObject $task -Force | Out-Null
        Say "  done"
    }
}

$legacy = Get-ShotsTask $LegacyTaskName
if ($legacy) {
    if ($RemoveLegacyTask) {
        if ($PSCmdlet.ShouldProcess("$TaskPath$LegacyTaskName", "Unregister deprecated task")) {
            Unregister-ScheduledTask -TaskName $LegacyTaskName -TaskPath $TaskPath -Confirm:$false
            Say "removed the deprecated '$LegacyTaskName' task (replaced by '$WorkerTaskName')"
        }
    } else {
        Say "NOTE: the deprecated '$LegacyTaskName' task (scripts\scheduled_scan.ps1) is still registered and would run updates twice; rerun with -RemoveLegacyTask to remove it."
    }
}

if ($StartNow) {
    foreach ($name in @($WebTaskName, $WorkerTaskName)) {
        if ($PSCmdlet.ShouldProcess("$TaskPath$name", "Start task now")) {
            Start-ScheduledTask -TaskName $name -TaskPath $TaskPath
            Say "started '$name'"
        }
    }
} else {
    Say "Tasks start at the next $(if ($AtStartup) { 'boot' } else { 'logon' }); add -StartNow to start them now."
}
Say "Check with: powershell -NoProfile -ExecutionPolicy Bypass -File deploy\status.ps1"
