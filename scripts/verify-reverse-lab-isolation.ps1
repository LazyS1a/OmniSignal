$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & docker compose -f 'compose.reverse-lab.yaml' run --build --rm --entrypoint python lab-gate -m omnisignal.reverse_lab.isolation_probe
    if ($LASTEXITCODE -ne 0) { throw 'Reverse-lab isolation verification failed.' }
} finally {
    Pop-Location
}
