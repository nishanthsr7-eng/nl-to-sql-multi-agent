<#
.SYNOPSIS
    Run the baseline ladder and commit the artefacts. Intended for Task Scheduler.

.DESCRIPTION
    Phase 3 task 8 asked for a nightly eval job. It is a scheduled task on this
    machine rather than a GitHub Actions workflow for one reason: the generator is
    a local Ollama model on http://localhost:11434, so a GitHub-hosted runner
    cannot execute the suite at all. The portable half of the task -- the
    regression gate -- does run in CI, against the artefacts this script commits
    (see .github/workflows/eval-gate.yml and evals/gate.py).

    What this script does NOT do is push. It commits to a local branch and stops.
    An unattended job that force-pushes an accuracy number at 3am is how a bad run
    becomes the baseline before anyone has looked at it; `git log` the morning
    after is cheap.

    A degraded run (the provider was down and every generation fell through to the
    deterministic templates) is refused by `sqe eval` itself and never reaches
    evals/results/, so this script does not need to check for it -- but it does
    check that something new actually appeared, because "committed nothing" and
    "committed a good run" should not look the same in the log.

.PARAMETER Suite
    Which gold suite to run. Defaults to "gold".

.PARAMETER Branch
    Branch to commit onto. Defaults to "eval/nightly", so an unattended run never
    lands on main.

.EXAMPLE
    # Register it to run at 02:30 every night:
    $action  = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File D:\Files\Projects\Semantic_Query_Engine\scripts\nightly_eval.ps1"
    $trigger = New-ScheduledTaskTrigger -Daily -At 2:30AM
    Register-ScheduledTask -TaskName "sqe-nightly-eval" -Action $action -Trigger $trigger
#>

[CmdletBinding()]
param(
    [string]$Suite = "gold",
    [string]$Branch = "eval/nightly"
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "No venv interpreter at $python -- run: python -m pip install -e `".[dev]`""
}

$logDir = Join-Path $repo "evals\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMddTHHmmss"
$log = Join-Path $logDir "$stamp-nightly.log"

function Write-Log([string]$Message) {
    $line = "$(Get-Date -Format o)  $Message"
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

Write-Log "Starting nightly ladder run (suite=$Suite)."

# The generator is local; if it is not up there is nothing to measure and a run
# would either fail or, worse, degrade to the templates.
try {
    Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -TimeoutSec 5 -UseBasicParsing | Out-Null
} catch {
    Write-Log "Ollama is not reachable on :11434 -- skipping. ($($_.Exception.Message))"
    exit 0
}

# The star schema is generated and gitignored; a machine that has been cleaned
# would otherwise fail deep inside the first case.
& $python scripts\build_star_schema.py 2>&1 | Tee-Object -FilePath $log -Append

$before = @(Get-ChildItem "evals\results\*.json" -ErrorAction SilentlyContinue).Count

& $python -m semantic_query_engine.cli.main eval --suite $Suite --baseline ladder 2>&1 |
    Tee-Object -FilePath $log -Append
$evalExit = $LASTEXITCODE

$after = @(Get-ChildItem "evals\results\*.json" -ErrorAction SilentlyContinue).Count
$new = $after - $before
Write-Log "sqe eval exited $evalExit; $new new artefact(s)."

# Exit 1 from `sqe eval` is a containment breach or a failed accuracy gate. Both
# are findings worth keeping, so the artefacts still get committed -- the gate in
# CI will fail on them, which is the point.
if ($new -le 0) {
    Write-Log "No new artefacts (the run degraded, or it did not finish). Nothing to commit."
    exit $evalExit
}

git rev-parse --verify $Branch *> $null
if ($LASTEXITCODE -eq 0) { git checkout $Branch } else { git checkout -b $Branch }

git add evals/results
git commit -m @"
Nightly eval: $Suite ladder, $stamp

Recorded by scripts/nightly_eval.ps1 on $env:COMPUTERNAME. Not pushed; review
with git show, and let the CI gate (evals/gate.py) run, before merging.
"@ 2>&1 | Tee-Object -FilePath $log -Append

Write-Log "Committed $new artefact(s) to $Branch. Not pushed, by design."

& $python -m evals.gate 2>&1 | Tee-Object -FilePath $log -Append
$gateExit = $LASTEXITCODE
Write-Log "Regression gate exited $gateExit."

exit $gateExit
