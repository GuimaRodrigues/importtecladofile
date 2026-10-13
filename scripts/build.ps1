[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Assert-NativeSuccess {
    param([string]$Operation)
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE."
    }
}

$version = python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Assert-NativeSuccess "Python version detection"
if ([version]$version -lt [version]"3.12") {
    throw "Python 3.12 or newer is required; found $version."
}

$buildVenv = Join-Path $projectRoot ".build-venv"
$buildPython = Join-Path $buildVenv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $buildPython)) {
    python -m venv $buildVenv
    Assert-NativeSuccess "Build virtual environment creation"
}

& $buildPython -m pip install -e ".[dev,build]"
Assert-NativeSuccess "Build dependency installation"
$buildRoot = Join-Path $projectRoot "build"
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
$pytestTemp = Join-Path $buildRoot ("pytest-temp-{0}" -f [guid]::NewGuid().ToString("N"))
& $buildPython -m pytest -q -p no:cacheprovider `
    --basetemp $pytestTemp
Assert-NativeSuccess "Automated tests"
$buildID = [guid]::NewGuid().ToString("N")
$guiWorkPath = Join-Path $buildRoot ("pyinstaller-gui-{0}" -f $buildID)
$cliWorkPath = Join-Path $buildRoot ("pyinstaller-cli-{0}" -f $buildID)
& $buildPython -m PyInstaller --noconfirm --clean --name YamahaLibrarySubsetter --windowed `
    --workpath $guiWorkPath --paths src `
    --add-data "LICENSE;." --add-data "NOTICE.md;." --add-data "LICENSES;LICENSES" `
    scripts/gui_entry.py
Assert-NativeSuccess "GUI build"
& $buildPython -m PyInstaller --noconfirm --clean --name yamaha-library --console `
    --workpath $cliWorkPath --paths src `
    --add-data "LICENSE;." --add-data "NOTICE.md;." --add-data "LICENSES;LICENSES" `
    scripts/cli_entry.py
Assert-NativeSuccess "CLI build"

Write-Host "Executables created in $projectRoot\dist"
