# Start the local worker and the web app together, then stop both on Ctrl+C.
# Usage: powershell -ExecutionPolicy Bypass -File scripts/dev.ps1

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not (Test-Path "apps/web/.next")) {
  Write-Host "First run: dependencies must be installed." -ForegroundColor Yellow
  Write-Host "  pnpm install"
  Write-Host "  cd apps/worker; python -m venv .venv; .venv\Scripts\pip install -e `".[dev]`"; cd ../.."
  Write-Host ""
}

Write-Host "Starting worker on http://127.0.0.1:8788" -ForegroundColor Cyan
$worker = Start-Process -FilePath "node" -ArgumentList "scripts/worker.mjs","dev" -NoNewWindow -PassThru

try {
  Write-Host "Starting web app on http://localhost:3000" -ForegroundColor Cyan
  & pnpm --filter "@clipforge/web" dev
} finally {
  if ($worker -and -not $worker.HasExited) {
    Write-Host "Stopping worker..." -ForegroundColor DarkGray
    Stop-Process -Id $worker.Id -Force -ErrorAction SilentlyContinue
  }
}
