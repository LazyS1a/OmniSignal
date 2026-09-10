$ErrorActionPreference = "Stop"

. "$PSScriptRoot\use-d-drive.ps1"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not installed or not available on PATH."
}

if ([string]::IsNullOrWhiteSpace($env:OMNISIGNAL_DB_PASSWORD)) {
    throw "Set OMNISIGNAL_DB_PASSWORD in the current terminal. It will not be written to a project file."
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$dataDirectory = Join-Path $projectRoot "data\postgres"
$appDataDirectory = Join-Path $projectRoot "data\app"
$backupDirectory = Join-Path $projectRoot "backups"
New-Item -ItemType Directory -Force -Path $dataDirectory, $appDataDirectory, $backupDirectory | Out-Null

Push-Location $projectRoot
try {
    docker compose config --quiet
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose configuration validation failed." }
    docker compose up --build --detach
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose startup failed." }

    $apiPort = if ($env:OMNISIGNAL_API_PORT) { $env:OMNISIGNAL_API_PORT } else { "8010" }
    $readyUrl = "http://127.0.0.1:$apiPort/health/ready"
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            $response = Invoke-RestMethod -Uri $readyUrl -TimeoutSec 2
            if ($response.status -eq "ready") {
                Write-Output "OmniSignal is ready at http://127.0.0.1:$apiPort"
                exit 0
            }
        } catch {
            if ($attempt -eq 30) { throw "API did not become ready. Run: docker compose logs api db" }
            Start-Sleep -Seconds 2
        }
    }
} finally {
    Pop-Location
}
