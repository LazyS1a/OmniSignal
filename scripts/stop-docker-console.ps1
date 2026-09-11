$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$secretPath = Join-Path $projectRoot 'artifacts\private\docker-console\secrets.json'

$previousPassword = $env:OMNISIGNAL_DB_PASSWORD
$previousToken = $env:OMNISIGNAL_LOCAL_CONSOLE_TOKEN
$previousPrincipals = $env:OMNISIGNAL_CONTROL_PRINCIPALS_JSON
$previousDataDir = $env:OMNISIGNAL_DATA_DIR
$previousAppDataDir = $env:OMNISIGNAL_APP_DATA_DIR
try {
    if (Test-Path -LiteralPath $secretPath -PathType Leaf) {
        $secrets = Get-Content -LiteralPath $secretPath -Raw | ConvertFrom-Json
        $env:OMNISIGNAL_DB_PASSWORD = [string]$secrets.database_password
        $env:OMNISIGNAL_LOCAL_CONSOLE_TOKEN = [string]$secrets.operator_token
    } else {
        $env:OMNISIGNAL_DB_PASSWORD = 'unused-during-stop'
        $env:OMNISIGNAL_LOCAL_CONSOLE_TOKEN = 'unused-during-stop'
    }
    $env:OMNISIGNAL_CONTROL_PRINCIPALS_JSON = '[]'
    $env:OMNISIGNAL_DATA_DIR = Join-Path $projectRoot 'artifacts\private\docker-console\postgres'
    $env:OMNISIGNAL_APP_DATA_DIR = Join-Path $projectRoot 'artifacts\private\docker-console\app'
    Push-Location $projectRoot
    try {
        & docker compose down --remove-orphans
        if ($LASTEXITCODE -ne 0) { throw 'Docker console stop failed.' }
    } finally {
        Pop-Location
    }
    Write-Output 'OmniSignal Docker console stopped. Database and application data were preserved.'
} finally {
    $env:OMNISIGNAL_DB_PASSWORD = $previousPassword
    $env:OMNISIGNAL_LOCAL_CONSOLE_TOKEN = $previousToken
    $env:OMNISIGNAL_CONTROL_PRINCIPALS_JSON = $previousPrincipals
    $env:OMNISIGNAL_DATA_DIR = $previousDataDir
    $env:OMNISIGNAL_APP_DATA_DIR = $previousAppDataDir
}
