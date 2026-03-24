$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$backendDir = $root
$frontendDir = Join-Path $root "dashboard\react_dashboard"

if (-not (Test-Path $frontendDir)) {
    throw "Frontend directory not found: $frontendDir"
}

Write-Host "Starting Free Quant Scalping AI in LIVE mode..." -ForegroundColor Cyan
Write-Host "Backend: simulation OFF, PORT=8001" -ForegroundColor Gray
Write-Host "Frontend: VITE_API_URL=http://localhost:8001" -ForegroundColor Gray

# Backend window
$backendCmd = @(
    "`$Host.UI.RawUI.WindowTitle = 'FQSAI Backend (LIVE)'",
    "Set-Location `"$backendDir`"",
    "`$env:SIMULATION_MODE='0'",
    "if (Test-Path Env:SIMULATION_SPEED) { Remove-Item Env:SIMULATION_SPEED -ErrorAction SilentlyContinue }",
    "`$env:PORT='8001'",
    ".\run.ps1"
) -join "; "

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $backendCmd
) | Out-Null

# Frontend window
$frontendCmd = @(
    "`$Host.UI.RawUI.WindowTitle = 'FQSAI Dashboard (LIVE)'",
    "Set-Location `"$frontendDir`"",
    "`$env:VITE_API_URL='http://localhost:8001'",
    "if (-not (Test-Path 'node_modules')) { npm install }",
    "npm run dev"
) -join "; "

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $frontendCmd
) | Out-Null

Write-Host "Launched backend + frontend in separate windows." -ForegroundColor Green
Write-Host "Backend docs: http://localhost:8001/docs" -ForegroundColor Yellow
Write-Host "Dashboard URL will be shown in frontend terminal output." -ForegroundColor Yellow
