param(
    [ValidateRange(1, 65535)][int]$Port = 8010,
    [ValidateRange(0.5, 10)][double]$TimeoutSeconds = 3
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"
$projectRoot = Split-Path -Parent $PSScriptRoot
$reportDirectory = Join-Path $projectRoot 'artifacts\private\health'
New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
$reportName = 'health-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8) + '.json'
$reportPath = Join-Path $reportDirectory $reportName
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $projectRoot 'src'
try {
    & (Join-Path $projectRoot '.venv\Scripts\python.exe') -m omnisignal.operations.health --port $Port --timeout $TimeoutSeconds.ToString([Globalization.CultureInfo]::InvariantCulture) --report $reportPath
    $checkExitCode = $LASTEXITCODE
    Write-Output "Health evidence: $reportPath"
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
exit $checkExitCode
