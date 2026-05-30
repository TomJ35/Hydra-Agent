#Requires -Version 5.1
<#
.SYNOPSIS
    Execute tous les tests Hydra-Agent et affiche un resume.

.PARAMETER FailFast
    Arrete l'execution au premier echec.

.PARAMETER EnvFile
    Chemin vers le fichier .env a charger avant les tests.

.EXAMPLE
    .\run_all_tests.ps1
    .\run_all_tests.ps1 -FailFast
    .\run_all_tests.ps1 -EnvFile ".env.local"
#>
[CmdletBinding()]
param(
    [switch]$FailFast,
    [string]$EnvFile = ".env"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:StepResults = [System.Collections.Generic.List[PSCustomObject]]::new()
$script:RunStart = Get-Date

function Import-DotEnv {
    param([string]$Path = ".env")

    if (-not (Test-Path $Path)) {
        Write-Host "   [i] Fichier '$Path' introuvable, variables .env ignorees." -ForegroundColor DarkGray
        return
    }

    $count = 0
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith('#')) { return }

        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { return }

        $key = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim()

        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        if ($key) {
            [System.Environment]::SetEnvironmentVariable($key, $value, 'Process')
            $count++
        }
    }

    Write-Host "   [i] $count variable(s) chargee(s) depuis '$Path'." -ForegroundColor DarkGray
}

function Show-Summary {
    $failed = @($script:StepResults | Where-Object { $_.Status -eq 'FAIL' })
    $totalSec = [Math]::Round(((Get-Date) - $script:RunStart).TotalSeconds, 2)

    Write-Host ""
    Write-Host "------------------------------------------" -ForegroundColor DarkGray
    Write-Host " Resume des tests" -ForegroundColor White
    Write-Host "------------------------------------------" -ForegroundColor DarkGray

    foreach ($result in $script:StepResults) {
        $line = "  [$($result.Status)] $($result.Step)  (code=$($result.ExitCode), $($result.DurationSec)s)"
        if ($result.Status -eq 'PASS') {
            Write-Host $line -ForegroundColor Green
        }
        else {
            Write-Host $line -ForegroundColor Red
            if ($result.Message) {
                Write-Host "        $($result.Message)" -ForegroundColor Red
            }
        }
    }

    Write-Host "------------------------------------------" -ForegroundColor DarkGray
    Write-Host ("  {0} etape(s)  |  {1} echec(s)  |  duree totale : {2}s" -f `
        $script:StepResults.Count, $failed.Count, $totalSec) -ForegroundColor White
    Write-Host "------------------------------------------" -ForegroundColor DarkGray
}

function Invoke-Step {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Action
    )

    Write-Host ""
    Write-Host "==> $Name" -ForegroundColor Cyan

    $start = Get-Date
    $status = 'PASS'
    $exitCode = 0
    $message = ''

    try {
        & $Action

        $nativeExit = $LASTEXITCODE
        if ($null -ne $nativeExit -and $nativeExit -ne 0) {
            $status = 'FAIL'
            $exitCode = $nativeExit
            $message = "La commande a retourne le code $nativeExit"
        }
    }
    catch {
        $status = 'FAIL'
        $message = $_.Exception.Message

        $nativeExit = $LASTEXITCODE
        $exitCode = if ($null -ne $nativeExit -and $nativeExit -gt 0) {
            $nativeExit
        } else {
            1
        }
    }

    $duration = (Get-Date) - $start

    if ($status -eq 'PASS') {
        Write-Host "   -> PASS ($([Math]::Round($duration.TotalSeconds, 2))s)" -ForegroundColor Green
    }
    else {
        Write-Host "   -> FAIL (code=$exitCode, $([Math]::Round($duration.TotalSeconds, 2))s)" -ForegroundColor Red
        if ($message) {
            Write-Host "      $message" -ForegroundColor Red
        }
    }

    $script:StepResults.Add([PSCustomObject]@{
        Step = $Name
        Status = $status
        ExitCode = $exitCode
        DurationSec = [Math]::Round($duration.TotalSeconds, 2)
        Message = $message
    })

    if ($FailFast -and $status -eq 'FAIL') {
        Write-Host ""
        Write-Host "==> FailFast active : arret apres l'echec de '$Name'." -ForegroundColor Yellow
        Show-Summary
        exit 1
    }
}

Import-DotEnv -Path $EnvFile

Invoke-Step -Name "Agent Python tests (Pytest)" -Action {
    if (-not (Test-Path ".\tests")) {
        throw "Dossier tests introuvable."
    }
    python -m pytest -q
}

if (Test-Path ".\run_extra_tests.ps1") {
    Invoke-Step -Name "Extra tests" -Action {
        & ".\run_extra_tests.ps1"
    }
}

Show-Summary

$failed = @($script:StepResults | Where-Object { $_.Status -eq 'FAIL' })
if ($failed.Count -gt 0) {
    exit 1
}

Write-Host ""
Write-Host "==> Tous les tests sont passes." -ForegroundColor Green
