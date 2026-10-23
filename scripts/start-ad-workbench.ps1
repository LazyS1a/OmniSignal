param([int]$Port = 8503, [switch]$EnableWorker)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend/dist/index.html'))) {
    throw '先在 frontend 目录执行 npm ci 和 npm run build。'
}
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:OMNISIGNAL_WORKBENCH_DIR = Join-Path $projectRoot 'data/ad-workbench'
$env:OMNISIGNAL_WORKBENCH_WORKER = if ($EnableWorker) { 'true' } else { 'false' }
& uv run uvicorn omnisignal.workbench.app:create_app --factory --host 127.0.0.1 --port $Port --workers 1 --timeout-graceful-shutdown 180
exit $LASTEXITCODE
