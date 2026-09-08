param(
    [string]$Spec = 'examples\connectors\static_html.yaml',
    [string]$Policy = 'examples\policies\static_html.yaml',
    [string]$Registry = 'governance\source_registry.yaml'
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Project environment is missing. Run the locked environment sync first.'
}
$specPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Spec))
$policyPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Policy))
$registryPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Registry))
if (-not (Test-Path -LiteralPath $specPath -PathType Leaf)) { throw 'Connector spec file does not exist.' }
if (-not (Test-Path -LiteralPath $policyPath -PathType Leaf)) { throw 'Web policy file does not exist.' }
if (-not (Test-Path -LiteralPath $registryPath -PathType Leaf)) { throw 'Source registry file does not exist.' }

$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Push-Location $projectRoot
try {
    & $python -m omnisignal.connectors.static_web_cli --spec $specPath --policy $policyPath --registry $registryPath
    if ($LASTEXITCODE -ne 0) { throw "Static web connector stopped safely (exit code $LASTEXITCODE)." }
} finally {
    Pop-Location
}
