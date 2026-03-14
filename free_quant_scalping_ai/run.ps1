# Run the app using the venv's Python directly (no activation needed).
# If port 8000 is in use: $env:PORT=8001; .\run.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
$pip = Join-Path $root ".venv\Scripts\pip.exe"

if (-not (Test-Path $py)) {
    Write-Host "Creating venv with Python 3.11..."
    & py -3.11 -m venv ".venv"
}

Write-Host "Installing dependencies if needed..."
& $pip install -q -r requirements-minimal.txt 2>$null
if ($LASTEXITCODE -ne 0) { & $pip install -r requirements-minimal.txt }

Write-Host "Starting server..."
& $py main.py
