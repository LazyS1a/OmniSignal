param(
    [string]$Policy = 'examples\policies\public_search_signals.yaml',
    [string]$Sqlite = ''
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
Push-Location $projectRoot
try {
    $collectorArgs = @('-m', 'omnisignal.connectors.public_search_cli', '--policy', $Policy)
    if ($Sqlite) { $collectorArgs += @('--sqlite', $Sqlite) }
    & '.venv\Scripts\python.exe' @collectorArgs
    if ($LASTEXITCODE -ne 0) { throw "Public search collection stopped (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
}
