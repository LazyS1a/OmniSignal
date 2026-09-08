param(
    [Parameter(Mandatory = $true)]
    [ValidateLength(1, 256)]
    [string]$Query,
    [ValidatePattern('^[A-Z][A-Z0-9_]*$')]
    [string]$TokenEnv
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Project environment is missing. Run scripts\test-local.ps1 after creating .venv.'
}

$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$arguments = @('-m', 'omnisignal.connectors.github_cli', '--query', $Query)
if ($TokenEnv) {
    $tokenValue = [Environment]::GetEnvironmentVariable($TokenEnv, 'Process')
    if ([string]::IsNullOrWhiteSpace($tokenValue)) {
        throw "The requested token environment variable is empty in this PowerShell session."
    }
    $arguments += @('--token-env', $TokenEnv)
}

Push-Location $projectRoot
try {
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw "GitHub connector stopped safely (exit code $LASTEXITCODE)." }
} finally {
    Pop-Location
}
