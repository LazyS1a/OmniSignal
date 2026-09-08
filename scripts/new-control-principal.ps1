param(
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$')]
    [string]$Actor = 'local-operator',

    [ValidateSet('viewer', 'operator', 'admin')]
    [string]$Role = 'operator'
)

$ErrorActionPreference = 'Stop'

$bytes = [byte[]]::new(32)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
$tokenBytes = [System.Text.Encoding]::UTF8.GetBytes($token)
$digest = [Convert]::ToHexString(
    [System.Security.Cryptography.SHA256]::HashData($tokenBytes)
).ToLowerInvariant()
$principals = @(
    @{
        actor = $Actor
        role = $Role
        token_sha256 = $digest
    }
)
$configuration = ConvertTo-Json -InputObject $principals -Compress
$escapedConfiguration = $configuration.Replace("'", "''")

Write-Output 'Copy both lines into the same PowerShell window before starting the API:'
Write-Output "`$env:OMNISIGNAL_CONTROL_PRINCIPALS_JSON = '$escapedConfiguration'"
Write-Output "`$env:OMNISIGNAL_CONTROL_TOKEN = '$token'"
Write-Output 'The API receives only the digest configuration. Paste OMNISIGNAL_CONTROL_TOKEN into the UI, then remove it from the shell.'
