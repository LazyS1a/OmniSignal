param(
    [ValidateRange(1024, 65535)]
    [int]$ApiPort = 18010,
    [ValidateRange(30, 600)]
    [int]$ReadyTimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http
. "$PSScriptRoot\use-d-drive.ps1"
. "$PSScriptRoot\lib\compose-container.ps1"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runId = [Guid]::NewGuid().ToString('N')
$shortId = $runId.Substring(0, 12)
$composeProject = "omnisignal-verify-$shortId"
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
$evidenceDirectory = Join-Path $projectRoot "artifacts\private\clean-environment\run-$stamp-$($runId.Substring(0, 8))"
$dataRoot = Join-Path $projectRoot "artifacts\private\clean-environment-data\$runId"
$postgresData = Join-Path $dataRoot 'postgres'
$appData = Join-Path $dataRoot 'app'
$reportPath = Join-Path $evidenceDirectory 'evidence.json'
$temporaryPassword = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
$previousPassword = $env:OMNISIGNAL_DB_PASSWORD
$previousData = $env:OMNISIGNAL_DATA_DIR
$previousAppData = $env:OMNISIGNAL_APP_DATA_DIR
$previousPort = $env:OMNISIGNAL_API_PORT
$previousScheduler = $env:OMNISIGNAL_SCHEDULER_ENABLED
$composeStarted = $false
$startedAt = [DateTimeOffset]::UtcNow

function Wait-Ready([int]$Port, [int]$TimeoutSeconds) {
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    $url = "http://127.0.0.1:$Port/health/ready"
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri $url -TimeoutSec 3
            if ($response.status -eq 'ready') { return }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    throw 'Clean-environment API did not become ready in time.'
}

function Wait-DatabaseFailure([int]$Port, [int]$TimeoutSeconds) {
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    $client = [System.Net.Http.HttpClient]::new()
    try {
        while ([DateTimeOffset]::UtcNow -lt $deadline) {
            try {
                $live = $client.GetAsync("http://127.0.0.1:$Port/health/live").GetAwaiter().GetResult()
                $ready = $client.GetAsync("http://127.0.0.1:$Port/health/ready").GetAwaiter().GetResult()
                $body = $ready.Content.ReadAsStringAsync().GetAwaiter().GetResult()
                if ([int]$live.StatusCode -eq 200 -and [int]$ready.StatusCode -eq 503) {
                    $document = $body | ConvertFrom-Json
                    if ($document.status -eq 'not_ready' -and $document.dependency -eq 'database') { return }
                }
            } catch { }
            Start-Sleep -Milliseconds 250
        }
    } finally {
        $client.Dispose()
    }
    throw 'API did not fail closed while the disposable database was unavailable.'
}

function Invoke-BoundedReadSmoke([int]$Port, [int]$Rounds = 25) {
    $client = [System.Net.Http.HttpClient]::new()
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        for ($round = 0; $round -lt $Rounds; $round++) {
            foreach ($path in @('/health/ready', '/ops/summary?window_hours=24')) {
                $response = $client.GetAsync("http://127.0.0.1:$Port$path").GetAwaiter().GetResult()
                if ([int]$response.StatusCode -ne 200) { throw 'Bounded read smoke received a non-success response.' }
                $null = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
            }
        }
    } finally {
        $clock.Stop()
        $client.Dispose()
    }
    return [ordered]@{ requests = $Rounds * 2; elapsed_seconds = [Math]::Round($clock.Elapsed.TotalSeconds, 3) }
}

function Remove-VerifiedDataDirectory([string]$Target, [string]$AllowedRoot) {
    if (-not (Test-Path -LiteralPath $Target)) { return }
    $resolvedTarget = (Resolve-Path -LiteralPath $Target).Path.TrimEnd('\')
    $resolvedRoot = (Resolve-Path -LiteralPath $AllowedRoot).Path.TrimEnd('\')
    if (-not $resolvedTarget.StartsWith($resolvedRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to clean a directory outside the clean-environment data root.'
    }
    if ((Split-Path -Parent $resolvedTarget) -ne $resolvedRoot) {
        throw 'Refusing to clean a nested or ambiguous data directory.'
    }
    Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
}

New-Item -ItemType Directory -Path $evidenceDirectory, $postgresData, $appData -Force | Out-Null
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $ApiPort)
try {
    $listener.Start()
} catch {
    throw "Local port $ApiPort is already in use. Choose another -ApiPort."
} finally {
    $listener.Stop()
}

try {
    $env:OMNISIGNAL_DB_PASSWORD = $temporaryPassword
    $env:OMNISIGNAL_DATA_DIR = $postgresData
    $env:OMNISIGNAL_APP_DATA_DIR = $appData
    $env:OMNISIGNAL_API_PORT = [string]$ApiPort
    $env:OMNISIGNAL_SCHEDULER_ENABLED = 'false'

    Push-Location $projectRoot
    try {
        & docker compose --project-name $composeProject config --quiet
        if ($LASTEXITCODE -ne 0) { throw 'Clean-environment Compose validation failed.' }
        & docker compose --project-name $composeProject up --build --detach db api
        if ($LASTEXITCODE -ne 0) { throw 'Clean-environment Compose startup failed.' }
        $composeStarted = $true
    } finally {
        Pop-Location
    }

    $firstReadyStarted = [DateTimeOffset]::UtcNow
    Wait-Ready $ApiPort $ReadyTimeoutSeconds
    $firstReadySeconds = [Math]::Round(([DateTimeOffset]::UtcNow - $firstReadyStarted).TotalSeconds, 3)

    $databaseContainer = Resolve-ComposeContainer -Project $composeProject -Service 'db'
    & docker stop --time 10 $databaseContainer | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Stopping the disposable database for the outage drill failed.' }
    Wait-DatabaseFailure $ApiPort 15
    $databaseRecoveryStarted = [DateTimeOffset]::UtcNow
    & docker start $databaseContainer | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Restarting the disposable database after the outage drill failed.' }
    Wait-Ready $ApiPort $ReadyTimeoutSeconds
    $databaseRecoverySeconds = [Math]::Round(([DateTimeOffset]::UtcNow - $databaseRecoveryStarted).TotalSeconds, 3)

    $schedule = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/ops/snapshot-schedules" -TimeoutSec 5
    if ($schedule.auto_start -ne $false -or $schedule.runtime_state -ne 'disabled_by_environment') {
        throw 'Scheduler safety gates were not closed in the clean environment.'
    }
    if (@($schedule.items | Where-Object { $_.eligible_to_trigger }).Count -ne 0) {
        throw 'A snapshot schedule unexpectedly became eligible in the clean environment.'
    }
    $readSmoke = Invoke-BoundedReadSmoke $ApiPort

    $apiContainer = Resolve-ComposeContainer -Project $composeProject -Service 'api'
    $restartStarted = [DateTimeOffset]::UtcNow
    & docker restart $apiContainer | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Clean-environment API restart failed.' }
    Wait-Ready $ApiPort $ReadyTimeoutSeconds
    $restartSeconds = [Math]::Round(([DateTimeOffset]::UtcNow - $restartStarted).TotalSeconds, 3)

    $revision = (& docker exec $databaseContainer psql -U omnisignal -d omnisignal -tAc 'SELECT version_num FROM alembic_version ORDER BY version_num;').Trim()
    $jobCount = [int]((& docker exec $databaseContainer psql -U omnisignal -d omnisignal -tAc 'SELECT count(*) FROM collection_jobs;').Trim())
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($revision) -or $jobCount -ne 0) {
        throw 'Clean-environment database verification failed.'
    }

    $evidence = [ordered]@{
        status = 'verified'
        scope = 'clean_compose_environment'
        started_at = $startedAt.ToString('o')
        finished_at = [DateTimeOffset]::UtcNow.ToString('o')
        schema_revision = $revision
        first_ready_seconds = $firstReadySeconds
        database_outage_failed_closed = $true
        database_recovery_seconds = $databaseRecoverySeconds
        api_restart_seconds = $restartSeconds
        scheduler_runtime_state = $schedule.runtime_state
        schedule_auto_start = $schedule.auto_start
        collection_job_count = $jobCount
        bounded_read_smoke_requests = $readSmoke.requests
        bounded_read_smoke_seconds = $readSmoke.elapsed_seconds
        collection_started = $false
        source_data_mounted = $false
        note = 'A unique Compose project, empty database directory and loopback-only port were used.'
    }
    [IO.File]::WriteAllText($reportPath, ($evidence | ConvertTo-Json -Depth 4) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    Write-Output "Clean environment verified: $reportPath"
} finally {
    $cleanupFailure = $null
    if ($composeStarted) {
        $savedErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'SilentlyContinue'
        Push-Location $projectRoot
        try {
            & docker compose --project-name $composeProject down --volumes --remove-orphans *> $null
            if ($LASTEXITCODE -ne 0) { $cleanupFailure = 'Disposable Compose project cleanup failed.' }
        } finally {
            Pop-Location
            $ErrorActionPreference = $savedErrorAction
        }
    }
    $allowedDataRoot = Join-Path $projectRoot 'artifacts\private\clean-environment-data'
    if (Test-Path -LiteralPath $allowedDataRoot) {
        try { Remove-VerifiedDataDirectory -Target $dataRoot -AllowedRoot $allowedDataRoot }
        catch { $cleanupFailure = 'Disposable data directory cleanup failed.' }
    }
    $env:OMNISIGNAL_DB_PASSWORD = $previousPassword
    $env:OMNISIGNAL_DATA_DIR = $previousData
    $env:OMNISIGNAL_APP_DATA_DIR = $previousAppData
    $env:OMNISIGNAL_API_PORT = $previousPort
    $env:OMNISIGNAL_SCHEDULER_ENABLED = $previousScheduler
    $temporaryPassword = $null
    if ($cleanupFailure) { throw $cleanupFailure }
}
