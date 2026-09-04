<#
.SYNOPSIS
    Runs the LedgerLoop demo on Windows. No API key required.

.DESCRIPTION
    The same four steps as `make demo`, for machines without make. Generates a seeded
    three-source batch, runs the full cascade over it, and writes results/report.html.

    Tier 3 is served entirely from the committed response cache in fixtures/llm_cache, so
    this works with no API key, no network, and produces byte-identical output on every
    machine. Disconnect your wifi and run it again if you want to prove that.

.EXAMPLE
    .\demo.ps1
#>

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# `pip install -e .` puts the console script in the user Scripts directory, which Python
# does not add to PATH on Windows. Rather than making that the reader's problem, find it.
if (-not (Get-Command ledgerloop -ErrorAction SilentlyContinue)) {
    # Select-Object -First 1 rather than [0]: Where-Object returns a bare string when only
    # one path matches, and indexing a string yields its first character. The first version
    # of this script printed "using C" and then failed to find the command.
    $scriptsDir = @(
        (Join-Path $env:APPDATA "Python\Python313\Scripts"),
        (Join-Path $env:APPDATA "Python\Python312\Scripts"),
        (Join-Path $env:APPDATA "Python\Python311\Scripts"),
        (Join-Path (Split-Path (Get-Command python).Source) "Scripts")
    ) | Where-Object { Test-Path (Join-Path $_ "ledgerloop.exe") } | Select-Object -First 1

    if (-not $scriptsDir) {
        Write-Error "ledgerloop is not installed. Run: python -m pip install -e "".[dev]"""
    }
    $env:Path = "$scriptsDir;$env:Path"
    Write-Host "using $scriptsDir" -ForegroundColor DarkGray
}

# Matches are append-only and a run id is unique (ADR-013), so reconciling into a run that
# already exists is refused. Clearing the database is what lets this be run twice --
# rehearsal, then the take that counts.
if (Test-Path "ledgerloop.db") { Remove-Item "ledgerloop.db" }

Write-Host ""
Write-Host "1/3  generating a seeded three-source batch" -ForegroundColor Cyan
ledgerloop generate --fixture adversarial --records 250

Write-Host ""
Write-Host "2/3  running the cascade" -ForegroundColor Cyan
ledgerloop reconcile --run-id demo --fixture adversarial

Write-Host ""
Write-Host "3/3  scoring against ground truth" -ForegroundColor Cyan
ledgerloop report --run-id demo --fixture adversarial --html results/report.html

Write-Host ""
Write-Host "Report written to results/report.html" -ForegroundColor Green
Write-Host "Open it to step through a Tier 3 adjudication gate by gate." -ForegroundColor DarkGray
