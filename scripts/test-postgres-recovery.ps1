param(
    [ValidatePattern('^[a-z0-9][a-z0-9_.-]{0,62}$')]
    [string]$SourceProject = 'omnisignal',
    [string]$OutputDirectory,
    [ValidateRange(10, 300)]
    [int]$ReadyTimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"
. "$PSScriptRoot\lib\compose-container.ps1"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runId = [Guid]::NewGuid().ToString('N')
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
    $OutputDirectory = Join-Path $projectRoot "backups\postgres\drill-$stamp-$($runId.Substring(0, 8))"
}
if (Test-Path -LiteralPath $OutputDirectory) { throw 'Recovery drill output directory already exists.' }
New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
$resolvedOutput = (Resolve-Path -LiteralPath $OutputDirectory).Path
$backupPath = Join-Path $resolvedOutput 'backup.dump'
$evidencePath = Join-Path $resolvedOutput 'evidence.json'

$sourceContainer = $null
$drillContainer = "omnisignal-pg-drill-$($runId.Substring(0, 12))"
$drillVolume = "omnisignal-pg-drill-$runId"
$sourceTemporary = "/tmp/source-$runId.dump"
$restoreTemporary = "/tmp/restore-$runId.dump"
$temporaryPassword = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
$createdContainer = $false
$createdVolume = $false
$startedAt = [DateTimeOffset]::UtcNow

function Wait-Postgres([string]$Container, [int]$TimeoutSeconds) {
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        & docker exec $Container pg_isready -U omnisignal -d omnisignal *> $null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Milliseconds 500
    }
    throw 'Disposable PostgreSQL instance did not become ready in time.'
}

try {
    $sourceContainer = Resolve-ComposeContainer -Project $SourceProject -Service 'db'
    $sourceImage = (& docker inspect $sourceContainer --format '{{.Config.Image}}').Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($sourceImage)) { throw 'Could not identify the source database image.' }

    & docker exec $sourceContainer pg_dump -U omnisignal -d omnisignal --format=custom --file=$sourceTemporary
    if ($LASTEXITCODE -ne 0) { throw 'Source database backup failed.' }
    & docker cp "${sourceContainer}:$sourceTemporary" $backupPath
    if ($LASTEXITCODE -ne 0) { throw 'Copying the database backup failed.' }
    $backupSha256 = (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash.ToLowerInvariant()

    & docker volume create --label 'omnisignal.scope=recovery-drill' $drillVolume | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Creating the disposable recovery volume failed.' }
    $createdVolume = $true
    & docker run --detach --name $drillContainer --label 'omnisignal.scope=recovery-drill' `
        --env 'POSTGRES_DB=omnisignal' --env 'POSTGRES_USER=omnisignal' --env "POSTGRES_PASSWORD=$temporaryPassword" `
        --volume "${drillVolume}:/var/lib/postgresql" $sourceImage | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Starting the disposable recovery database failed.' }
    $createdContainer = $true
    Wait-Postgres $drillContainer $ReadyTimeoutSeconds

    $restoreStarted = [DateTimeOffset]::UtcNow
    & docker cp $backupPath "${drillContainer}:$restoreTemporary"
    if ($LASTEXITCODE -ne 0) { throw 'Copying the backup into the disposable database failed.' }
    & docker exec $drillContainer pg_restore -U omnisignal -d omnisignal --exit-on-error --no-owner $restoreTemporary
    if ($LASTEXITCODE -ne 0) { throw 'Restoring the backup into the disposable database failed.' }
    $revision = (& docker exec $drillContainer psql -U omnisignal -d omnisignal -tAc 'SELECT version_num FROM alembic_version ORDER BY version_num;').Trim()
    $tableCount = [int]((& docker exec $drillContainer psql -U omnisignal -d omnisignal -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';").Trim())
    if ($LASTEXITCODE -ne 0 -or $tableCount -lt 4 -or [string]::IsNullOrWhiteSpace($revision)) {
        throw 'Restored database schema verification failed.'
    }
    $restoreSeconds = [Math]::Round(([DateTimeOffset]::UtcNow - $restoreStarted).TotalSeconds, 3)

    $restartStarted = [DateTimeOffset]::UtcNow
    & docker restart $drillContainer | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Restarting the disposable recovery database failed.' }
    Wait-Postgres $drillContainer $ReadyTimeoutSeconds
    $postRestartRevision = (& docker exec $drillContainer psql -U omnisignal -d omnisignal -tAc 'SELECT version_num FROM alembic_version ORDER BY version_num;').Trim()
    if ($LASTEXITCODE -ne 0 -or $postRestartRevision -ne $revision) { throw 'Schema revision changed after database restart.' }
    $restartSeconds = [Math]::Round(([DateTimeOffset]::UtcNow - $restartStarted).TotalSeconds, 3)

    $evidence = [ordered]@{
        status = 'verified'
        scope = 'independent_local_postgresql_recovery_drill'
        started_at = $startedAt.ToString('o')
        finished_at = [DateTimeOffset]::UtcNow.ToString('o')
        backup_sha256 = $backupSha256
        schema_revision = $revision
        public_table_count = $tableCount
        measured_restore_seconds = $restoreSeconds
        measured_restart_seconds = $restartSeconds
        source_untouched = $true
        schedule_started = $false
        collection_started = $false
        production_rpo_established = $false
        production_rto_established = $false
        note = 'Timing applies only to this local disposable drill and current data size.'
    }
    $json = $evidence | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($evidencePath, $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    Write-Output "PostgreSQL recovery drill verified: $evidencePath"
} finally {
    if ($sourceContainer) { & docker exec $sourceContainer rm -f $sourceTemporary 2>$null | Out-Null }
    if ($createdContainer -and $drillContainer.StartsWith('omnisignal-pg-drill-')) {
        & docker rm --force $drillContainer 2>$null | Out-Null
    }
    if ($createdVolume -and $drillVolume.StartsWith('omnisignal-pg-drill-')) {
        & docker volume rm $drillVolume 2>$null | Out-Null
    }
    $temporaryPassword = $null
}
