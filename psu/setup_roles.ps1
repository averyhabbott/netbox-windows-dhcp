<#
.SYNOPSIS
    Creates DHCPReader and DHCPWriter PSU roles and one App Token for each.

.DESCRIPTION
    Run from any PowerShell session with HTTPS access to the PSU server.
    Requires an existing PSU Administrator App Token to authenticate.

    Roles are created only if they do not already exist (safe to re-run).
    Tokens are created only if no token with that identity name already exists;
    if one does, you will be instructed to assign the role manually in the PSU UI.

    Token values are printed once — copy them into NetBox immediately.
    They cannot be retrieved again after this session.

.PARAMETER BaseUrl
    PSU server base URL, e.g. https://dhcp01.example.com:8443

.PARAMETER AdminToken
    An existing PSU Administrator App Token used to authenticate setup calls.

.PARAMETER LifespanDays
    Number of days before the generated tokens expire. Default: 365.

.EXAMPLE
    .\setup_roles.ps1 -BaseUrl https://dhcp01.example.com:8443 -AdminToken eyJ...
#>
param(
    [Parameter(Mandatory)][string]$BaseUrl,
    [Parameter(Mandatory)][string]$AdminToken,
    [int]$LifespanDays = 365
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$headers  = @{ Authorization = "Bearer $AdminToken"; 'Content-Type' = 'application/json' }
$splatSsl = if ($PSVersionTable.PSVersion.Major -ge 7) {
    @{ SkipCertificateCheck = $true }
} else {
    [Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
    @{}
}

function Invoke-PSU {
    param([string]$Method, [string]$Path, [hashtable]$Body)
    $irmArgs = @{ Method = $Method; Uri = "$BaseUrl$Path"; Headers = $headers } + $splatSsl
    if ($Body) { $irmArgs['Body'] = ($Body | ConvertTo-Json -Depth 5 -Compress) }
    Invoke-RestMethod @irmArgs
}

# ── Roles ──────────────────────────────────────────────────────────────────
Write-Host "`n=== Creating Roles ===" -ForegroundColor Cyan

foreach ($r in @(
    @{ name = 'DHCPReader'; description = 'Read-only access to DHCP API GET endpoints.' },
    @{ name = 'DHCPWriter'; description = 'Full read/write access to all DHCP API endpoints.' }
)) {
    try {
        Invoke-PSU GET "/api/v1/role/$($r.name)" | Out-Null
        Write-Host "  [skip] Role '$($r.name)' already exists." -ForegroundColor Yellow
    } catch {
        Invoke-PSU POST '/api/v1/role' $r | Out-Null
        Write-Host "  [ok]   Created role '$($r.name)'." -ForegroundColor Green
    }
}

# ── Tokens ─────────────────────────────────────────────────────────────────
Write-Host "`n=== Creating App Tokens ===" -ForegroundColor Cyan

$expiration = (Get-Date).ToUniversalTime().AddDays($LifespanDays).ToString('o')
$existing   = @((Invoke-PSU GET '/api/v1/apptoken') | ForEach-Object { $_.identity.name })
$results    = @{}

foreach ($t in @(
    @{ identity = 'NetBox-DHCP-Read';  role = 'DHCPReader' },
    @{ identity = 'NetBox-DHCP-Write'; role = 'DHCPWriter'  }
)) {
    if ($existing -contains $t.identity) {
        Write-Host "  [skip] Token '$($t.identity)' already exists." -ForegroundColor Yellow
        Write-Host "         To assign the '$($t.role)' role: Security > App Tokens > edit > Role." -ForegroundColor Yellow
        $results[$t.identity] = $null
    } else {
        $body = @{
            identity   = @{ name = $t.identity; system = $true }
            role       = $t.role
            expiration = $expiration
        }
        $obj = Invoke-PSU POST '/api/v1/apptoken/grant' $body
        $results[$t.identity] = $obj.token
        Write-Host "  [ok]   Created token '$($t.identity)' (expires in $($LifespanDays) days)." -ForegroundColor Green
    }
}

# ── Output ─────────────────────────────────────────────────────────────────
Write-Host "`n=== Token Values -- copy into NetBox now ===" -ForegroundColor Cyan
Write-Host "(Token values cannot be retrieved after this session.)`n"

foreach ($t in @('NetBox-DHCP-Read', 'NetBox-DHCP-Write')) {
    if ($results[$t]) {
        Write-Host "${t}:" -ForegroundColor Green
        Write-Host "  $($results[$t])`n"
    }
}

Write-Host "Done." -ForegroundColor Cyan
