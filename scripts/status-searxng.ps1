param(
    [ValidateRange(0, 500)][int]$LogLines = 0
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:SEARXNG_SECRET = 'status-command-placeholder-not-applied-to-container'
Push-Location $projectRoot
try {
    & docker compose -f compose.searxng.yaml ps
    if ($LASTEXITCODE -ne 0) { throw "SearXNG status check failed." }
    if ($LogLines -gt 0) {
        & docker compose -f compose.searxng.yaml logs --tail $LogLines --no-color searxng
        if ($LASTEXITCODE -ne 0) { throw "SearXNG log read failed." }
    }
} finally {
    Remove-Item Env:SEARXNG_SECRET -ErrorAction SilentlyContinue
    Pop-Location
}
