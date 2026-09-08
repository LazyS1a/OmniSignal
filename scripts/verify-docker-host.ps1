$ErrorActionPreference = "Stop"

$installationDirectory = "D:\Applications\Docker"
$dataDirectory = "D:\DockerData\wsl"
$desktopPath = Join-Path $installationDirectory "Docker Desktop.exe"
$dockerPath = Join-Path $installationDirectory "resources\bin\docker.exe"

if (-not (Test-Path -LiteralPath $desktopPath -PathType Leaf)) {
    throw "Docker Desktop is not installed at the approved D drive location."
}
if (-not (Test-Path -LiteralPath $dockerPath -PathType Leaf)) {
    throw "Docker CLI is missing from the approved D drive location."
}
if (-not (Test-Path -LiteralPath $dataDirectory -PathType Container)) {
    throw "Docker WSL data root is missing from D drive."
}

& $dockerPath version
if ($LASTEXITCODE -ne 0) {
    throw "Docker engine is not ready. Open Docker Desktop and complete first-run setup."
}

& $dockerPath info --format "engine={{.ServerVersion}} storage={{.Driver}}"
if ($LASTEXITCODE -ne 0) { throw "docker info failed." }

$localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$dockerAppData = Join-Path $localAppData "Docker"
$unexpectedVhd = Get-ChildItem -LiteralPath $dockerAppData -Recurse -File -Filter "*.vhdx" -ErrorAction SilentlyContinue
if ($unexpectedVhd) {
    $paths = $unexpectedVhd.FullName -join "; "
    throw "Docker VHDX unexpectedly exists on C drive: $paths"
}

$approvedVhd = Get-ChildItem -LiteralPath $dataDirectory -Recurse -File -Filter "*.vhdx" -ErrorAction SilentlyContinue
if (-not $approvedVhd) {
    throw "No Docker VHDX was found under the approved D drive data root."
}

Write-Output "Docker host verified: program and WSL disk are on D drive."
$approvedVhd | Select-Object FullName,Length,LastWriteTime
