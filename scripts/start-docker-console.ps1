param(
    [ValidateRange(1024, 65535)][int]$ApiPort = 8010,
    [ValidateRange(1024, 65535)][int]$UiPort = 8501
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$privateRoot = Join-Path $projectRoot 'artifacts\private\docker-console'
$secretPath = Join-Path $privateRoot 'secrets.json'

function New-RandomHex([int]$ByteCount) {
    $bytes = New-Object byte[] $ByteCount
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToHexString($bytes).ToLowerInvariant()
}

function Get-OrCreateSecrets {
    if (Test-Path -LiteralPath $secretPath -PathType Leaf) {
        try { $document = Get-Content -LiteralPath $secretPath -Raw | ConvertFrom-Json } catch {
            throw 'Docker console secret file is invalid. Move it aside and retry.'
        }
        if ($document.schema_version -ne '1.0' -or
            $document.database_password -notmatch '^[a-f0-9]{64}$' -or
            $document.operator_token -notmatch '^[a-f0-9]{64}$') {
            throw 'Docker console secret file failed validation. Move it aside and retry.'
        }
        return $document
    }

    New-Item -ItemType Directory -Force -Path $privateRoot | Out-Null
    $document = [ordered]@{
        schema_version = '1.0'
        database_password = New-RandomHex 32
        operator_token = New-RandomHex 32
    }
    $temporary = Join-Path $privateRoot ('.secrets.' + [guid]::NewGuid().ToString('N') + '.tmp')
    $json = ConvertTo-Json -InputObject $document -Compress
    [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $secretPath
    return [pscustomobject]$document
}

function Get-Sha256([string]$Value) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}

& docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Desktop is not ready. Start Docker Desktop, wait for Engine running, then retry.'
}

$secrets = Get-OrCreateSecrets
$variables = @(
    'OMNISIGNAL_DB_PASSWORD',
    'OMNISIGNAL_API_PORT',
    'OMNISIGNAL_UI_PORT',
    'OMNISIGNAL_DATA_DIR',
    'OMNISIGNAL_APP_DATA_DIR',
    'OMNISIGNAL_LOCAL_CONSOLE_TOKEN',
    'OMNISIGNAL_CONTROL_PRINCIPALS_JSON'
)
$previous = @{}
foreach ($name in $variables) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }

try {
    $env:OMNISIGNAL_DB_PASSWORD = $secrets.database_password
    $env:OMNISIGNAL_API_PORT = [string]$ApiPort
    $env:OMNISIGNAL_UI_PORT = [string]$UiPort
    $env:OMNISIGNAL_DATA_DIR = Join-Path $privateRoot 'postgres'
    $env:OMNISIGNAL_APP_DATA_DIR = Join-Path $privateRoot 'app'
    $env:OMNISIGNAL_LOCAL_CONSOLE_TOKEN = $secrets.operator_token
    $principal = @([ordered]@{
        actor = 'docker-console'
        role = 'operator'
        token_sha256 = Get-Sha256 $secrets.operator_token
    })
    $env:OMNISIGNAL_CONTROL_PRINCIPALS_JSON = ConvertTo-Json -InputObject $principal -Compress

    $apiUrl = "http://127.0.0.1:$ApiPort/health/ready"
    $uiUrl = "http://127.0.0.1:$UiPort"

    Push-Location $projectRoot
    try {
        $runningServices = @(& docker compose ps --status running --services 2>$null)
        $alreadyRunning = @('db', 'api', 'ui') | Where-Object { $_ -notin $runningServices }
        if ($alreadyRunning.Count -eq 0) {
            try {
                $existingApi = Invoke-RestMethod -Method Get -Uri $apiUrl -TimeoutSec 3
                $existingUi = Invoke-WebRequest -UseBasicParsing -Uri "$uiUrl/_stcore/health" -TimeoutSec 3
            } catch {
                $existingApi = $null
                $existingUi = $null
            }
            if ($existingApi.status -eq 'ready' -and $existingUi.StatusCode -eq 200) {
                Start-Process -FilePath $uiUrl
                Write-Output "OmniSignal Docker console is already running at $uiUrl"
                return
            }
        }

        $occupied = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalPort -in @($ApiPort, $UiPort) }
        if ($occupied) {
            $ports = ($occupied.LocalPort | Sort-Object -Unique) -join ', '
            throw "Local port is already in use: $ports. Close the other service or choose different ports."
        }

        & docker compose up -d --build --wait
        if ($LASTEXITCODE -ne 0) { throw 'Docker console failed to become healthy.' }
    } finally {
        Pop-Location
    }

    $api = Invoke-RestMethod -Method Get -Uri $apiUrl -TimeoutSec 5
    if ($api.status -ne 'ready') { throw 'OmniSignal API did not report ready.' }
    $ui = Invoke-WebRequest -UseBasicParsing -Uri "$uiUrl/_stcore/health" -TimeoutSec 5
    if ($ui.StatusCode -ne 200) { throw 'OmniSignal UI did not report healthy.' }
    Start-Process -FilePath $uiUrl
    Write-Output "OmniSignal Docker console opened at $uiUrl"
} finally {
    foreach ($name in $variables) {
        if ($null -eq $previous[$name]) {
            [Environment]::SetEnvironmentVariable($name, $null, 'Process')
        } else {
            [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
        }
    }
}
