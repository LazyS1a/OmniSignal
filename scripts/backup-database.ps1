param(
    [string]$OutputPath,
    [ValidatePattern('^[a-z0-9][a-z0-9_.-]{0,62}$')]
    [string]$ComposeProject = 'omnisignal'
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\use-d-drive.ps1"
. "$PSScriptRoot\lib\compose-container.ps1"
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputPath = Join-Path $projectRoot "backups\omnisignal-$timestamp.dump"
}

$parent = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $parent | Out-Null
if (Test-Path -LiteralPath $OutputPath) { throw 'Backup destination already exists.' }

$container = $null
$temporaryPath = "/tmp/omnisignal-backup-$([Guid]::NewGuid().ToString('N')).dump"
try {
    $container = Resolve-ComposeContainer -Project $ComposeProject -Service 'db'
    docker exec $container pg_dump -U omnisignal -d omnisignal --format=custom --file=$temporaryPath
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed." }
    docker cp "${container}:$temporaryPath" $OutputPath
    if ($LASTEXITCODE -ne 0) { throw "Copying the backup to the host failed." }
    docker exec $container rm -f $temporaryPath | Out-Null
    Write-Output "Backup created: $((Resolve-Path -LiteralPath $OutputPath).Path)"
} finally {
    if ($container) { docker exec $container rm -f $temporaryPath 2>$null | Out-Null }
}
