function Resolve-ComposeContainer {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[a-z0-9][a-z0-9_.-]{0,62}$')]
        [string]$Project,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[a-z0-9][a-z0-9_.-]{0,62}$')]
        [string]$Service,

        [switch]$IncludeStopped
    )

    $arguments = @('ps')
    if ($IncludeStopped) { $arguments += '--all' }
    $arguments += @(
        '--filter', "label=com.docker.compose.project=$Project",
        '--filter', "label=com.docker.compose.service=$Service",
        '--format', '{{.ID}}'
    )
    $containers = @(& docker @arguments | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($LASTEXITCODE -ne 0) { throw 'Docker container lookup failed.' }
    if ($containers.Count -ne 1) {
        throw "Expected exactly one Docker container for project '$Project' service '$Service'; found $($containers.Count)."
    }
    return $containers[0].Trim()
}
