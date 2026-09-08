param(
    [switch]$Pull
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

& "$PSScriptRoot\verify-docker-host.ps1" | Out-Host

$secretBytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Fill($secretBytes)
$env:SEARXNG_SECRET = [Convert]::ToHexString($secretBytes).ToLowerInvariant()

Push-Location $projectRoot
try {
    if ($Pull) {
        & docker compose -f compose.searxng.yaml pull
        if ($LASTEXITCODE -ne 0) { throw "SearXNG image pull failed." }
    }
    & docker compose -f compose.searxng.yaml up -d --wait
    if ($LASTEXITCODE -ne 0) { throw "SearXNG container failed to become healthy." }
    $probe = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8888/search?q=ChatGPT&format=json&engines=brave%2Cduckduckgo&language=en-US&safesearch=1&pageno=1' -TimeoutSec 35
    if ($null -eq $probe.results) { throw "SearXNG JSON API contract is unavailable." }
    if (@($probe.results).Count -lt 1) { throw "SearXNG JSON API is ready, but the upstream search probe returned no results." }
    $unresponsiveCount = @($probe.unresponsive_engines).Count
    Write-Output "SearXNG is ready at http://127.0.0.1:8888 ($(@($probe.results).Count) upstream results; $unresponsiveCount unresponsive engines)."
} finally {
    Remove-Item Env:SEARXNG_SECRET -ErrorAction SilentlyContinue
    Pop-Location
}
