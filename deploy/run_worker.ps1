<#
.SYNOPSIS
  Task action of "Shots Worker": one worker cycle (python -m shots.worker run-once), logged.

.DESCRIPTION
  Appends to reports\logs\worker_<date>.log, deletes logs older than -KeepLogDays, and stops the
  cycle (with its child processes) after -TimeoutMinutes, below the task's 30 minute limit, so a
  hung cycle never lingers. The worker itself takes the overlap lock and skips when nothing is due.
  Exit code: the worker's (0 = skipped or ok, 1 = update failures, 124 = timed out).
#>
param(
    [int]$KeepLogDays = 14,
    [int]$TimeoutMinutes = 25,
    [switch]$Force
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

Remove-ShotsOldLogs -KeepDays $KeepLogDays
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
$pyArgs = @("-m", "shots.worker", "run-once")
if ($Force) { $pyArgs += "--force" }

$out = [IO.Path]::GetTempFileName()
$err = [IO.Path]::GetTempFileName()
Write-ShotsLog "worker" ("start: python {0}" -f ($pyArgs -join " "))
$code = 1
try {
    $p = Start-Process -FilePath $ShotsPython -ArgumentList $pyArgs -WorkingDirectory $ShotsRoot -NoNewWindow `
        -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    $null = $p.Handle  # keep a handle so ExitCode is available after exit
    if (-not $p.WaitForExit($TimeoutMinutes * 60 * 1000)) {
        & taskkill.exe /PID $p.Id /T /F | Out-Null
        Write-ShotsLog "worker" "timed out after $TimeoutMinutes min: stopped the cycle and its child processes"
        $code = 124
    } else {
        $p.WaitForExit()
        $code = $p.ExitCode
    }
} catch {
    Write-ShotsLog "worker" "could not start the worker: $($_.Exception.Message)"
} finally {
    foreach ($f in @($err, $out)) {
        if (Test-Path -LiteralPath $f) {
            $text = Get-Content -LiteralPath $f -Encoding UTF8
            if ($text) { Add-Content -LiteralPath (Get-ShotsDailyLog "worker") -Value $text -Encoding UTF8 }
            Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
        }
    }
}
Write-ShotsLog "worker" "end: exit code $code"
exit $code
