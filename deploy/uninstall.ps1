<#
.SYNOPSIS
  Remove the "Shots Web" and "Shots Worker" tasks (idempotent) and stop what they started.

.DESCRIPTION
  Stops the tasks, unregisters them, then stops any remaining `python -m shots.web` server process
  (the wrapper's child) unless -KeepRunning. Logs, the database and data files are left untouched.
  Use -WhatIf to see what it would do. No admin rights needed unless the tasks were installed with
  -AtStartup by an administrator.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\uninstall.ps1 -WhatIf
  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\uninstall.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param([switch]$KeepRunning)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

function Say([string]$m) { Write-Host "[uninstall] $m" }

foreach ($name in @($WebTaskName, $WorkerTaskName)) {
    $task = Get-ShotsTask $name
    if (-not $task) { Say "task '$name' is not registered: nothing to do"; continue }
    if ($task.State -eq "Running" -and $PSCmdlet.ShouldProcess("$TaskPath$name", "Stop running task")) {
        Stop-ScheduledTask -TaskName $name -TaskPath $TaskPath
        Say "stopped '$name'"
    }
    if ($PSCmdlet.ShouldProcess("$TaskPath$name", "Unregister scheduled task")) {
        Unregister-ScheduledTask -TaskName $name -TaskPath $TaskPath -Confirm:$false
        Say "removed '$name'"
    }
}

if (-not $KeepRunning) {
    $savedWhatIf = $WhatIfPreference; $WhatIfPreference = $false  # module load only defines aliases
    Import-Module CimCmdlets -Verbose:$false
    $WhatIfPreference = $savedWhatIf
    # A venv python.exe starts the real interpreter as a child: match both by command line.
    $procs = Get-CimInstance Win32_Process -Filter "Name like 'python%.exe' or Name like 'powershell%.exe'" |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match '-m shots\.web' -or $_.CommandLine -match 'deploy\\run_web\.ps1') }
    foreach ($p in $procs) {
        if ($PSCmdlet.ShouldProcess("pid $($p.ProcessId): $($p.CommandLine)", "Stop process")) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            Say "stopped pid $($p.ProcessId) ($($p.Name))"
        }
    }
    if (-not $procs) { Say "no shots.web process running" }
}
Say "Logs in $ShotsLogDir and the database in data\ are kept."
