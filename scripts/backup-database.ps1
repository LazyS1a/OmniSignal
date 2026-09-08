param(
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\use-d-drive.ps1"
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputPath = Join-Path $projectRoot "backups\omnisignal-$timestamp.dump"
}

$parent = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $parent | Out-Null

Push-Location $projectRoot
try {
    $container = docker compose ps --quiet db
    if ([string]::IsNullOrWhiteSpace($container)) { throw "Database container is not running." }
    $temporaryPath = "/tmp/omnisignal-backup.dump"
    docker exec $container pg_dump -U omnisignal -d omnisignal --format=custom --file=$temporaryPath
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed." }
    docker cp "${container}:$temporaryPath" $OutputPath
    if ($LASTEXITCODE -ne 0) { throw "Copying the backup to the host failed." }
    docker exec $container rm -f $temporaryPath | Out-Null
    Write-Output "Backup created: $((Resolve-Path -LiteralPath $OutputPath).Path)"
} finally {
    Pop-Location
}
