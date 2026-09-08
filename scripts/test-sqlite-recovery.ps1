param(
    [string]$SourcePath,
    [string]$OutputDirectory,
    [string]$ExpectedRevision
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $SourcePath) { $SourcePath = Join-Path $projectRoot 'data\omnisignal.db' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $projectRoot 'backups\sqlite' }
$sourceFile = Get-Item -LiteralPath $SourcePath
if ($sourceFile.PSIsContainer) { throw 'Source must be a SQLite file.' }
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
if ([System.IO.Path]::GetPathRoot($outputRoot) -ne 'D:\') {
    throw 'Recovery drill outputs must stay on D:.'
}
$runName = 'drill-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$runDirectory = Join-Path $outputRoot $runName
New-Item -ItemType Directory -Path $runDirectory | Out-Null
$backupPath = Join-Path $runDirectory 'backup.sqlite3'
$restorePath = Join-Path $runDirectory 'restored.sqlite3'
$reportPath = Join-Path $runDirectory 'evidence.json'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$timer = [Diagnostics.Stopwatch]::StartNew()
try {
    $backupArguments = @('-m', 'omnisignal.storage.backup', 'backup', $sourceFile.FullName, '--destination', $backupPath)
    if ($ExpectedRevision) { $backupArguments += @('--revision', $ExpectedRevision) }
    $backupJson = & $python @backupArguments
    if ($LASTEXITCODE -ne 0) { throw 'Backup verification failed; outputs are not usable.' }
    $backupEvidence = $backupJson | ConvertFrom-Json
    $restoreArguments = @('-m', 'omnisignal.storage.backup', 'restore', $backupPath, '--destination', $restorePath, '--sha256', $backupEvidence.file_sha256)
    if ($ExpectedRevision) { $restoreArguments += @('--revision', $ExpectedRevision) }
    $restoreJson = & $python @restoreArguments
    if ($LASTEXITCODE -ne 0) { throw 'Restore verification failed; outputs are not usable.' }
    $restoreEvidence = $restoreJson | ConvertFrom-Json
    $timer.Stop()
    [ordered]@{
        status = 'verified'
        database_only = $true
        elapsed_seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 3)
        backup_file = 'backup.sqlite3'
        restored_file = 'restored.sqlite3'
        backup = $backupEvidence
        restored = $restoreEvidence
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding utf8
    Write-Output "SQLite recovery verified. Original database was not replaced. Evidence: $reportPath"
} catch {
    [ordered]@{ status = 'failed'; outputs_usable = $false } |
        ConvertTo-Json | Set-Content -LiteralPath $reportPath -Encoding utf8
    throw 'SQLite recovery drill failed. Retained outputs are not verified backups; check evidence.json.'
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
