$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:SEARXNG_SECRET = 'stop-command-placeholder-not-used-by-container'
Push-Location $projectRoot
try {
    & docker compose -f compose.searxng.yaml down
    if ($LASTEXITCODE -ne 0) { throw "SearXNG container stop failed." }
} finally {
    Remove-Item Env:SEARXNG_SECRET -ErrorAction SilentlyContinue
    Pop-Location
}
