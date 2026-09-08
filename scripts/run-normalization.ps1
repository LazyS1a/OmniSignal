param(
    [string]$Config = 'config\normalization.yaml',
    [string]$ArchiveRoot = 'data\raw'
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$configPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Config))
$archivePath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $ArchiveRoot))
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Project environment is missing.' }
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { throw 'Normalization config does not exist.' }

$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Push-Location $projectRoot
try {
    & $python -m omnisignal.normalization.cli --config $configPath --archive-root $archivePath
    if ($LASTEXITCODE -ne 0) { throw "Normalization stopped safely (exit code $LASTEXITCODE)." }
} finally {
    Pop-Location
}
