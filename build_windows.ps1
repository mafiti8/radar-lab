# Builds the standalone Windows RadarLab.exe (see bin/radar_lab.spec).
# Run from inside a cloned copy of this repo, in PowerShell:
#   .\build_windows.ps1
#
# Written 2026-09-25 on a Linux dev box, for real execution on Windows --
# PyInstaller has to run on the platform it's targeting for a dependency
# stack this heavy (Py-ART/MetPy/netCDF4/pywebview), cross-building isn't
# realistic here. Expect to iterate on bin/radar_lab.spec's COLLECT_ALL
# list if the built .exe fails with a missing-module error the first few
# times -- that's the normal failure mode for packaging scientific Python
# stacks, not a sign this script did something wrong.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "RadarLab Windows build"
Write-Host "======================"
Write-Host ""

# ---------------------------------------------------------------------
# Python check
# ---------------------------------------------------------------------
$PythonBin = "python"
try {
    $pyVersionOutput = & $PythonBin -c "import sys; print('%d.%d' % sys.version_info[:2])"
} catch {
    Write-Error "Python not found on PATH. Install Python 3.10+ from python.org first (check 'Add python.exe to PATH' during install)."
    exit 1
}
Write-Host "Using Python $pyVersionOutput"

# ---------------------------------------------------------------------
# Virtual environment + dependencies
# ---------------------------------------------------------------------
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    & $PythonBin -m venv .venv
} else {
    Write-Host "Reusing existing .venv"
}

$VenvPip = ".venv\Scripts\pip.exe"
$VenvPyInstaller = ".venv\Scripts\pyinstaller.exe"

Write-Host "Installing runtime dependencies (numpy/scipy/Py-ART/MetPy -- can take several minutes)..."
& $VenvPip install --upgrade pip -q
& $VenvPip install -r bin\requirements.txt -q
# pygrib deliberately skipped here -- no Windows wheels on PyPI (see
# bin\requirements-mosaic.txt's own comment). The app runs fine without
# it, just without the national mosaic feature -- a real, accepted V1
# Windows limitation, not an oversight.
& $VenvPip install -r bin\requirements-desktop.txt -q
& $VenvPip install -r bin\requirements-build.txt -q
Write-Host "Dependencies installed."
Write-Host ""

# ---------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------
Write-Host "Building RadarLab.exe (PyInstaller -- can take several minutes, first run especially)..."
Push-Location bin
& $ScriptDir\.venv\Scripts\pyinstaller.exe radar_lab.spec --distpath ..\dist --workpath ..\build --noconfirm
Pop-Location

Write-Host ""
if (Test-Path "dist\RadarLab.exe") {
    Write-Host "Built: dist\RadarLab.exe"
    Write-Host "Run it directly, or from PowerShell: .\dist\RadarLab.exe"
    Write-Host ""
    Write-Host "First run will likely trigger a Windows SmartScreen warning" -ForegroundColor Yellow
    Write-Host "('Windows protected your PC') since this isn't code-signed --" -ForegroundColor Yellow
    Write-Host "that's expected for an unsigned indie app, not a build error." -ForegroundColor Yellow
    Write-Host "Click 'More info' -> 'Run anyway'." -ForegroundColor Yellow
} else {
    Write-Error "Build finished but dist\RadarLab.exe wasn't found -- check the PyInstaller output above for the real error."
    exit 1
}
