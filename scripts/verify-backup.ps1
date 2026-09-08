param(
    [Parameter(Mandatory = $true)]
    [string]$BackupPath
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\use-d-drive.ps1"
$projectRoot = Split-Path -Parent $PSScriptRoot
$resolvedBackup = Resolve-Path -LiteralPath $BackupPath
$verificationDatabase = "omnisignal_restore_verify"
$temporaryPath = "/tmp/omnisignal-restore-verify.dump"

Push-Location $projectRoot
try {
    $container = docker compose ps --quiet db
    if ([string]::IsNullOrWhiteSpace($container)) { throw "Database container is not running." }

    docker exec $container dropdb --if-exists -U omnisignal $verificationDatabase
    docker exec $container createdb -U omnisignal $verificationDatabase
    docker cp $resolvedBackup.Path "${container}:$temporaryPath"
    docker exec $container pg_restore -U omnisignal -d $verificationDatabase --exit-on-error $temporaryPath
    if ($LASTEXITCODE -ne 0) { throw "Backup restore verification failed." }

    $tableCount = docker exec $container psql -U omnisignal -d $verificationDatabase -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"
    if ([int]$tableCount -lt 4) { throw "Restored database is missing expected tables." }
    Write-Output "Backup verified in disposable database: tables=$tableCount"
} finally {
    if ($container) {
        docker exec $container dropdb --if-exists -U omnisignal $verificationDatabase | Out-Null
        docker exec $container rm -f $temporaryPath | Out-Null
    }
    Pop-Location
}
