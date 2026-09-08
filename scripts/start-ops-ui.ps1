$ErrorActionPreference = "Stop"

. "$PSScriptRoot\use-d-drive.ps1"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$entryPoint = Join-Path $projectRoot "src\omnisignal\ops_ui\app.py"

foreach ($required in @($python, $entryPoint)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required UI file is missing: $required"
    }
}

& $python -c "import streamlit" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Streamlit is not installed. Run: .\.bootstrap-venv\Scripts\uv.exe sync --frozen --extra crawl --extra ui"
}

$apiPort = if ($env:OMNISIGNAL_API_PORT) { $env:OMNISIGNAL_API_PORT } else { "8010" }
$uiPort = if ($env:OMNISIGNAL_UI_PORT) { $env:OMNISIGNAL_UI_PORT } else { "8501" }
if (-not $env:OMNISIGNAL_API_BASE_URL) {
    $env:OMNISIGNAL_API_BASE_URL = "http://127.0.0.1:$apiPort"
}
$env:PYTHONPATH = Join-Path $projectRoot "src"
$env:STREAMLIT_BROWSER_GATHER_USAGE_STATS = "false"

Push-Location $projectRoot
try {
    Write-Output "Starting OmniSignal read-only operations console at http://127.0.0.1:$uiPort"
    & $python -m streamlit run $entryPoint `
        --server.address 127.0.0.1 `
        --server.port $uiPort `
        --server.headless true `
        --browser.gatherUsageStats false
    if ($LASTEXITCODE -ne 0) { throw "Streamlit exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
