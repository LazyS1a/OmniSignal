param(
    [Parameter(Mandatory = $true)]
    [string]$BackupPath,
    [ValidatePattern('^[a-z0-9][a-z0-9_.-]{0,62}$')]
    [string]$ComposeProject = 'omnisignal'
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\use-d-drive.ps1"
. "$PSScriptRoot\lib\compose-container.ps1"
$resolvedBackup = Resolve-Path -LiteralPath $BackupPath
$runId = [Guid]::NewGuid().ToString('N')
$verificationDatabase = "omnisignal_restore_$($runId.Substring(0, 12))"
$temporaryPath = "/tmp/omnisignal-restore-$runId.dump"

$container = $null
try {
    $container = Resolve-ComposeContainer -Project $ComposeProject -Service 'db'

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
}
