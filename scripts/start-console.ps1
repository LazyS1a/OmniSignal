param(
    [ValidateRange(1024, 65535)][int]$ApiPort = 8010,
    [ValidateRange(1024, 65535)][int]$UiPort = 8501,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runtimeDirectory = Join-Path $projectRoot 'artifacts\private\console'
$statePath = Join-Path $runtimeDirectory 'runtime.json'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Project Python environment is missing.' }
New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null

function Get-LiveSupervisor([object]$State) {
    if (-not $State -or -not $State.supervisor_pid) { return $null }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($State.supervisor_pid)" -ErrorAction SilentlyContinue
    if ($process -and $process.CommandLine -like '*omnisignal.console_launcher*') { return $process }
    return $null
}

$supervisor = $null
$existingSupervisor = $false
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    try { $oldState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } catch { $oldState = $null }
    $liveSupervisor = Get-LiveSupervisor $oldState
    if ($liveSupervisor) {
        $supervisor = Get-Process -Id $liveSupervisor.ProcessId
        $existingSupervisor = $true
        if ($oldState.status -eq 'ready') {
            try {
                $health = Invoke-WebRequest -UseBasicParsing -Uri "$($oldState.ui_url)/_stcore/health" -TimeoutSec 2
            } catch { $health = $null }
            if ($health -and $health.StatusCode -eq 200) {
                if (-not $NoBrowser) { Start-Process -FilePath $oldState.ui_url }
                Write-Output "OmniSignal is already running at $($oldState.ui_url)"
                exit 0
            }
        }
    }
}

if (-not $supervisor) {
    $launchStartedAt = [DateTime]::UtcNow
    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    try {
        $arguments = @('-m', 'omnisignal.console_launcher', '--api-port', $ApiPort, '--ui-port', $UiPort)
        $launcherProcess = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
    } finally {
        $env:PYTHONPATH = $previousPythonPath
    }
}

$deadline = [DateTime]::UtcNow.AddSeconds(90)
do {
    Start-Sleep -Milliseconds 250
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        try { $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } catch { $state = $null }
        $stateFile = Get-Item -LiteralPath $statePath
        $stateIsCurrent = $existingSupervisor -or $stateFile.LastWriteTimeUtc -ge $launchStartedAt
        $liveStateSupervisor = Get-LiveSupervisor $state
        if ($state -and $stateIsCurrent -and $liveStateSupervisor) {
            if ($state.status -eq 'ready') {
                if (-not $NoBrowser) { Start-Process -FilePath $state.ui_url }
                Write-Output "OmniSignal console opened at $($state.ui_url)"
                exit 0
            }
            if ($state.status -eq 'failed') { throw "Console startup failed: $($state.message) Logs: $($state.log_directory)" }
        }
    }
    if (-not $existingSupervisor -and $launcherProcess.HasExited) {
        throw "Console launcher exited before readiness (exit $($launcherProcess.ExitCode))."
    }
    if ($existingSupervisor -and $supervisor.HasExited) {
        throw 'Existing console supervisor exited before readiness.'
    }
} while ([DateTime]::UtcNow -lt $deadline)
throw 'Console startup timed out. Check artifacts\private\console\runtime.json.'
