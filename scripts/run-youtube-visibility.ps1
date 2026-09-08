param(
    [string]$Policy = 'examples\policies\youtube_visibility.yaml',
    [string]$Sqlite = 'data\youtube-visibility-trial.db',
    [string]$Output = ''
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
Push-Location $projectRoot
try {
    $collectorArgs = @('-m', 'omnisignal.connectors.youtube_visibility_cli', '--policy', $Policy, '--sqlite', $Sqlite)
    if ($Output) { $collectorArgs += @('--output', $Output) }
    & '.venv\Scripts\python.exe' @collectorArgs
    if ($LASTEXITCODE -ne 0) { throw "Video search collection stopped (exit $LASTEXITCODE)." }
} finally { Pop-Location }
