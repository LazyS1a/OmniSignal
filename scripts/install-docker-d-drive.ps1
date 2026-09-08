$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated PowerShell terminal."
}

if (Test-Path -LiteralPath "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending") {
    throw "Windows still requires a reboot. Restart first, then run this script again."
}

$installerPath = "D:\DockerRuntime\installer\DockerDesktop-4.89.0-238018.exe"
$expectedSha256 = "854626704af28a160d5af68b96b3e32eacf08ab397ce6c12eb02a04788d73681"
$installationDirectory = "D:\Applications\Docker"
$dataDirectory = "D:\DockerData\wsl"
$temporaryDirectory = "D:\DockerRuntime\temp"

if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "Verified Docker Desktop installer is missing: $installerPath"
}

$actualSha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha256 -ne $expectedSha256) {
    throw "Docker Desktop installer checksum mismatch. Installation blocked."
}

$signature = Get-AuthenticodeSignature -FilePath $installerPath
if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "Docker Inc") {
    throw "Docker Desktop installer signature is not valid for Docker Inc. Installation blocked."
}

$wslOutput = (& wsl --version 2>&1 | Out-String)
$wslExitCode = $LASTEXITCODE
# Windows PowerShell may surface wsl.exe UTF-16 output with embedded NUL bytes.
$normalizedWslOutput = $wslOutput -replace "`0", ""
if ($wslExitCode -ne 0 -or $normalizedWslOutput -notmatch "2\.7\.12\.0|WSL") {
    throw "Modern WSL is not ready. Verify WSL after reboot before installing Docker Desktop."
}

New-Item -ItemType Directory -Force -Path $installationDirectory, $dataDirectory, $temporaryDirectory | Out-Null
$env:TEMP = $temporaryDirectory
$env:TMP = $temporaryDirectory

$arguments = @(
    "install",
    "--quiet",
    "--backend=wsl-2",
    "--installation-dir=$installationDirectory",
    "--wsl-default-data-root=$dataDirectory",
    "--no-windows-containers"
)

$process = Start-Process -FilePath $installerPath -ArgumentList $arguments -Wait -PassThru
if ($process.ExitCode -ne 0) {
    throw "Docker Desktop installer failed with exit code $($process.ExitCode)."
}

$desktopPath = Join-Path $installationDirectory "Docker Desktop.exe"
if (-not (Test-Path -LiteralPath $desktopPath -PathType Leaf)) {
    throw "Installer completed but Docker Desktop.exe was not found on D drive."
}

Write-Output "Docker Desktop installed on D drive."
Write-Output "Program: $desktopPath"
Write-Output "WSL data root: $dataDirectory"
Write-Output "Open Docker Desktop once to review and accept Docker's license terms, then run scripts/verify-docker-host.ps1."
