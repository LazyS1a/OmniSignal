$cacheRoot = 'D:\CodexCache\OmniSignal'
$projectRoot = Split-Path -Parent $PSScriptRoot
$projectTrivyCache = Join-Path $projectRoot '.tools\trivy-cache'

$env:UV_CACHE_DIR = Join-Path $cacheRoot 'uv'
$env:TRIVY_CACHE_DIR = if (Test-Path -LiteralPath (Join-Path $projectTrivyCache 'db\trivy.db') -PathType Leaf) {
    $projectTrivyCache
} else {
    Join-Path $cacheRoot 'trivy'
}
$env:TEMP = Join-Path $cacheRoot 'tmp'
$env:TMP = $env:TEMP

$approvedDockerBin = 'D:\Applications\Docker\resources\bin'
if (-not (Get-Command docker -ErrorAction SilentlyContinue) -and
    (Test-Path -LiteralPath (Join-Path $approvedDockerBin 'docker.exe') -PathType Leaf)) {
    $env:Path = "$approvedDockerBin;$env:Path"
}

New-Item -ItemType Directory -Force -Path $env:UV_CACHE_DIR, $env:TRIVY_CACHE_DIR, $env:TEMP | Out-Null

Write-Host "OmniSignal caches are on D:"
Write-Host "UV_CACHE_DIR=$env:UV_CACHE_DIR"
Write-Host "TRIVY_CACHE_DIR=$env:TRIVY_CACHE_DIR"
Write-Host "TEMP=$env:TEMP"
