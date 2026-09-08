param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('ghidra', 'jadx', 'apktool', 'frida', 'mitmproxy')]
    [string]$Tool
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Project environment is missing. Run scripts\test-local.ps1 after creating .venv.'
}

$env:PYTHONPATH = Join-Path $projectRoot 'src'
Push-Location $projectRoot
try {
    & $python -m omnisignal.reverse_lab.tool_installer $Tool
    if ($LASTEXITCODE -ne 0) { throw "Locked installation for $Tool was rejected." }
} finally {
    Pop-Location
}
