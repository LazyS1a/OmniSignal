param(
    [string]$Spec = 'examples\connectors\authorized_hook.yaml',
    [string]$Policy = 'examples\policies\authorized_hook.yaml',
    [string]$Registry = 'governance\source_registry.yaml',
    [switch]$ResetCircuit
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
foreach ($path in @($specPath, $policyPath, $registryPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required configuration does not exist: $path" }
}

$arguments = @('-m', 'omnisignal.connectors.authorized_hook_cli', '--spec', $specPath, '--policy', $policyPath, '--registry', $registryPath)
if ($ResetCircuit) { $arguments += '--reset-circuit' }
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Push-Location $projectRoot
try {
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Authorized Hook connector stopped safely (exit code $LASTEXITCODE)." }
} finally {
    Pop-Location
}
