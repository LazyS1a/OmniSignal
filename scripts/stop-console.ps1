$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$statePath = Join-Path $projectRoot 'artifacts\private\console\runtime.json'
if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    Write-Output 'OmniSignal console is not running.'
    exit 0
}
try { $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } catch { throw 'Console state file is unreadable.' }
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $($state.supervisor_pid)" -ErrorAction SilentlyContinue
if (-not $process) {
    Write-Output 'OmniSignal console is not running; the state file is stale.'
    exit 0
}
if ($process.CommandLine -notlike '*omnisignal.console_launcher*') {
    throw 'State PID belongs to another process; nothing was stopped.'
}
Stop-Process -Id $process.ProcessId
$processId = [int]$process.ProcessId
$deadline = [DateTime]::UtcNow.AddSeconds(15)
do {
    Start-Sleep -Milliseconds 250
    $alive = Get-Process -Id $processId -ErrorAction SilentlyContinue
} while ($alive -and [DateTime]::UtcNow -lt $deadline)
if ($alive) { throw 'Console supervisor did not stop in time.' }
$ports = @([int]$state.api_port, [int]$state.ui_port) | Select-Object -Unique
$portDeadline = [DateTime]::UtcNow.AddSeconds(10)
do {
    $listeners = foreach ($port in $ports) {
        Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    }
    if (-not $listeners) { break }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $portDeadline)
if ($listeners) { throw 'Console child ports did not close in time.' }
$state.status = 'stopped'
$state | Add-Member -NotePropertyName stopped_at_utc -NotePropertyValue ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) -Force
$temporaryState = "$statePath.tmp"
$state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporaryState -Encoding utf8
Move-Item -LiteralPath $temporaryState -Destination $statePath -Force
Write-Output 'OmniSignal console stopped. Logs remain on D:.'
