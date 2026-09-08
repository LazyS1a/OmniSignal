param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z][a-z0-9_]{2,63}$')]
    [string]$OutputName,
    [string]$Actor = 'local_operator'
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\use-d-drive.ps1"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$toolRoot = 'D:\ReverseLabTools\OmniSignal\jadx\1.5.6'
$java = Join-Path $toolRoot 'jre\bin\java.exe'
$jar = Join-Path $toolRoot 'lib\jadx-gui-1.5.6-all.jar'
$toolReceipt = Join-Path $toolRoot 'installation-receipt.json'
foreach ($required in @($python, $java, $jar, $toolReceipt)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required locked component is missing: $required"
    }
}
$installed = Get-Content -LiteralPath $toolReceipt -Raw | ConvertFrom-Json
if ($installed.tool -ne 'jadx' -or $installed.version -ne '1.5.6' -or
    $installed.asset_sha256 -ne '56a870460d03d3d6f22eb0908c33e298bc7370c952a6b7fa48c22a187ecd690b') {
    throw 'JADX installation receipt does not match the approved lock.'
}

$labRoot = Join-Path $projectRoot 'reverse_lab'
$manifestPath = [System.IO.Path]::GetFullPath($Manifest, $projectRoot)
$env:PYTHONPATH = Join-Path $projectRoot 'src'

Push-Location $projectRoot
try {
    $gateLines = @(& $python -m omnisignal.reverse_lab.cli --workspace $labRoot verify-sample --manifest $manifestPath --actor $Actor)
    if ($LASTEXITCODE -ne 0) { throw 'Sample gate rejected the JADX run.' }
    $gate = $gateLines[-1] | ConvertFrom-Json
    if ($gate.status -ne 'allowed') { throw 'Sample gate did not return allowed status.' }
    Write-Host $gateLines[-1]

    $sampleRelative = Join-Path 'reverse_lab' ($gate.relative_path -replace '/', '\')
    $outputRelative = Join-Path 'reverse_lab\artifacts' $OutputName
    $output = Join-Path $projectRoot $outputRelative
    $analysisReceipt = Join-Path $output 'analysis-receipt.json'
    if (Test-Path -LiteralPath $output) {
        if (Test-Path -LiteralPath $analysisReceipt -PathType Leaf) {
            $receipt = Get-Content -LiteralPath $analysisReceipt -Raw | ConvertFrom-Json
            if ($receipt.receipt_version -eq '1.1' -and $receipt.path_redaction -eq 'lab_root' -and
                $receipt.sample_sha256 -eq $gate.sha256 -and $receipt.tool_version -eq '1.5.6') {
                Write-Host "Existing verified analysis reused: $output"
                return
            }
        }
        throw 'Analysis output already exists without a matching receipt; refusing to overwrite.'
    }

    & $java --enable-native-access=ALL-UNNAMED -cp $jar jadx.cli.JadxCLI --no-debug-info -d $outputRelative $sampleRelative
    if ($LASTEXITCODE -ne 0) { throw "JADX failed with exit code $LASTEXITCODE" }

    $textExtensions = @('.java', '.kt', '.smali', '.xml', '.json', '.txt', '.properties')
    $forwardRoot = $projectRoot.Replace('\', '/')
    Get-ChildItem -LiteralPath $output -Recurse -File | Where-Object { $_.Extension -in $textExtensions } | ForEach-Object {
        $content = [System.IO.File]::ReadAllText($_.FullName)
        $redacted = $content.Replace($projectRoot, '<LAB_ROOT>').Replace($forwardRoot, '<LAB_ROOT>')
        if ($redacted -ne $content) {
            [System.IO.File]::WriteAllText($_.FullName, $redacted, [System.Text.UTF8Encoding]::new($false))
        }
    }

    $files = @(
        Get-ChildItem -LiteralPath $output -Recurse -File | Sort-Object FullName | ForEach-Object {
            [pscustomobject]@{
                path = [System.IO.Path]::GetRelativePath($output, $_.FullName).Replace('\', '/')
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                size = $_.Length
            }
        }
    )
    $receipt = [ordered]@{
        receipt_version = '1.1'
        tool = 'jadx'
        tool_version = '1.5.6'
        sample_id = $gate.sample_id
        sample_sha256 = $gate.sha256
        path_redaction = 'lab_root'
        output_files = $files
        created_at = [DateTimeOffset]::UtcNow.ToString('o')
    }
    $receipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $analysisReceipt -Encoding utf8
    Write-Host "JADX analysis completed: $output"
} finally {
    Pop-Location
}
