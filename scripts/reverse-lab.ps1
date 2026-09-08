param(
    [ValidateSet('VerifyFixtureIsolated', 'VerifyFixture', 'VerifySample', 'ApprovePromotion')]
    [string]$Action = 'VerifyFixtureIsolated',
    [string]$Manifest,
    [string]$Finding,
    [string]$Review,
    [string]$Actor = 'local_operator'
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"

$projectRoot = Split-Path -Parent $PSScriptRoot
$workspace = Join-Path $projectRoot 'reverse_lab'

if ($Action -eq 'VerifyFixtureIsolated') {
    Push-Location $projectRoot
    try {
        & docker compose -f 'compose.reverse-lab.yaml' run --build --rm lab-gate
        if ($LASTEXITCODE -ne 0) { throw 'Isolated reverse-lab gate rejected the fixture.' }
        return
    } finally {
        Pop-Location
    }
}

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Project environment is missing. Run scripts\test-local.ps1 after creating .venv.'
}

$env:PYTHONPATH = Join-Path $projectRoot 'src'

if ($Action -eq 'VerifyFixture') {
    $Manifest = Join-Path $workspace 'manifests\fixture_owned_client.yaml'
}

Push-Location $projectRoot
try {
    if ($Action -in @('VerifyFixture', 'VerifySample')) {
        if (-not $Manifest) { throw '-Manifest is required for VerifySample.' }
        & $python -m omnisignal.reverse_lab.cli --workspace $workspace verify-sample --manifest $Manifest --actor $Actor
    } else {
        if (-not $Manifest -or -not $Finding -or -not $Review) {
            throw '-Manifest, -Finding and -Review are required for ApprovePromotion.'
        }
        & $python -m omnisignal.reverse_lab.cli --workspace $workspace approve-promotion --manifest $Manifest --finding $Finding --review $Review --actor $Actor
    }
    if ($LASTEXITCODE -ne 0) { throw "Reverse-lab gate rejected the action (exit code $LASTEXITCODE)." }
} finally {
    Pop-Location
}
