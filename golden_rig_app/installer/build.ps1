<#
.SYNOPSIS
    Build the Golden Rig Windows installer.

.DESCRIPTION
    1. Installs runtime + build requirements into the Python venv
    2. Runs the unit tests
    3. Bundles GoldenRigServer.exe and rig_certtool.exe with PyInstaller (no Python needed on the rig)
    4. Compiles installer\GoldenRig.iss with Inno Setup 6 -> installer\Output\GoldenRigSetup-<version>.exe

    Inno Setup: winget install --id JRSoftware.InnoSetup -e

.EXAMPLE
    .\installer\build.ps1 -Version 1.0.0
#>
param(
    [string]$Version = '1.0.0',
    [string]$Python,
    [switch]$SkipTests,
    [switch]$SkipInstaller
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$app = Split-Path $here -Parent

if (-not $Python) {
    $Python = @((Join-Path $app '.venv\Scripts\python.exe'), (Join-Path (Split-Path $app -Parent) '.venv\Scripts\python.exe')) |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Python) { throw 'No .venv found. Create one: py -3.11 -m venv .venv' }
}
Write-Host "Python: $Python"

function Invoke-Checked([string]$exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "$exe $($arguments -join ' ') failed ($LASTEXITCODE)" }
}

Invoke-Checked $Python @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '-r', (Join-Path $app 'requirements.txt'), '-r', (Join-Path $here 'requirements-build.txt'))

if (-not $SkipTests) {
    Push-Location $app
    try { Invoke-Checked $Python @('-m', 'unittest', 'discover', '-s', 'tests', '-v') } finally { Pop-Location }
}

$buildDir = Join-Path $here 'build'
Invoke-Checked $Python @('-m', 'PyInstaller', '--noconfirm', '--clean',
    '--distpath', (Join-Path $buildDir 'dist'), '--workpath', (Join-Path $buildDir 'work'),
    (Join-Path $here 'golden_rig.spec'))

$dist = Join-Path $buildDir 'dist\GoldenRig'
Invoke-Checked (Join-Path $dist 'rig_certtool.exe') @('--help') | Out-Null
Write-Host "Bundled application: $dist" -ForegroundColor Green

if ($SkipInstaller) { return }
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source }
}
if (-not $iscc) {
    throw 'Inno Setup 6 (ISCC.exe) not found. Install it: winget install --id JRSoftware.InnoSetup -e'
}
Invoke-Checked $iscc @("/DAppVersion=$Version", (Join-Path $here 'GoldenRig.iss'))
Write-Host "Installer: $(Join-Path $here "Output\GoldenRigSetup-$Version.exe")" -ForegroundColor Green
