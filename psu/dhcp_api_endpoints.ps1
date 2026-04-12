<#
.SYNOPSIS
    PowerShell Universal endpoint definitions for the netbox-windows-dhcp plugin.

.DESCRIPTION
    Registers all DHCP API endpoints expected by the NetBox Windows DHCP plugin's
    PSUClient.  This script should be placed in your PowerShell Universal repository
    and loaded as an endpoint script.

    Requires: PowerShell Universal 5.x (tested on 5.6.11+)

    Prerequisites on the Windows DHCP server:
      - DhcpServer PowerShell module (installed with the DHCP Server role)
      - PowerShell Universal 5.x

    Authentication (PSU v5):
      PSU v5 uses JWT App Tokens — not X-API-Key headers.
      1. In the PSU admin console go to Security > App Tokens and generate a token.
      2. Paste that token into the "API Key" field on the DHCPServer object in NetBox.
         The plugin sends it as:  Authorization: Bearer <token>
      3. To enforce authentication on these endpoints, add -Authentication to each
         New-PSUEndpoint call below.

    All endpoints are rooted at /api/dhcp/ to match the PSUClient base URL.
#>


# ===========================================================================
# SHARED HELPERS  (dot-sourced into each endpoint via -ScriptBlock approach)
# These are defined as strings and invoked with [scriptblock]::Create() so
# that they work correctly inside PSU runspaces without a separate module.
# ===========================================================================

$HelperFunctions = {

    function ConvertTo-ScopeObject {
        param([Microsoft.Management.Infrastructure.CimInstance]$Scope)
        [ordered]@{
            scope_id               = $Scope.ScopeId.ToString()
            name                   = [string]$Scope.Name
            start_ip               = $Scope.StartRange.ToString()
            end_ip                 = $Scope.EndRange.ToString()
            subnet_mask            = $Scope.SubnetMask.ToString()
            description            = [string]$Scope.Description
            state                  = $Scope.State.ToString()
            lease_duration_seconds = [int]$Scope.LeaseDuration.TotalSeconds
        }
    }

    function ConvertTo-LeaseObject {
        param([Microsoft.Management.Infrastructure.CimInstance]$Lease)
        $expiry = $null
        if ($Lease.LeaseExpiryTime) {
            $expiry = $Lease.LeaseExpiryTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        }
        [ordered]@{
            ip_address    = $Lease.IPAddress.ToString()
            client_id     = [string]$Lease.ClientId
            hostname      = [string]$Lease.HostName
            scope_id      = $Lease.ScopeId.ToString()
            lease_expiry  = $expiry
            address_state = $Lease.AddressState.ToString()
        }
    }

    function ConvertTo-ReservationObject {
        param([Microsoft.Management.Infrastructure.CimInstance]$Reservation)
        [ordered]@{
            ip_address  = $Reservation.IPAddress.ToString()
            client_id   = [string]$Reservation.ClientId
            name        = [string]$Reservation.Name
            description = [string]$Reservation.Description
            type        = $Reservation.Type.ToString()
            scope_id    = $Reservation.ScopeId.ToString()
        }
    }

    function ConvertTo-FailoverObject {
        param([Microsoft.Management.Infrastructure.CimInstance]$Failover)
        $switchInterval = $null
        if ($Failover.StateSwitchInterval -and $Failover.StateSwitchInterval.TotalSeconds -gt 0) {
            $switchInterval = [int]$Failover.StateSwitchInterval.TotalSeconds
        }
        # Resolve the local server's FQDN so NetBox can match it to a DHCPServer object
        $localFqdn = try {
            [System.Net.Dns]::GetHostEntry([System.Net.Dns]::GetHostName()).HostName
        } catch {
            $env:COMPUTERNAME
        }
        [ordered]@{
            name                      = [string]$Failover.Name
            primary_server            = $localFqdn
            secondary_server          = [string]$Failover.PartnerServer
            mode                      = $Failover.Mode.ToString()
            scope_ids                 = @($Failover.ScopeId | ForEach-Object { $_.ToString() })
            max_client_lead_time      = [int]$Failover.MaxClientLeadTime.TotalSeconds
            max_response_delay        = [int]$Failover.MaxResponseDelay.TotalSeconds
            state_switchover_interval = $switchInterval
            enable_auth               = [bool]$Failover.EnableAuth
        }
    }

    function ConvertTo-OptionValueObject {
        param([Microsoft.Management.Infrastructure.CimInstance]$Option)
        [ordered]@{
            code         = [int]$Option.OptionId
            name         = [string]$Option.Name
            value        = @($Option.Value | ForEach-Object { $_.ToString() })
            type         = $Option.Type.ToString()
            vendor_class = [string]$Option.VendorClass
        }
    }

    function Find-ReservationByClientId {
        param([string]$ClientId)
        foreach ($scope in (Get-DhcpServerv4Scope -ErrorAction SilentlyContinue)) {
            $match = Get-DhcpServerv4Reservation -ScopeId $scope.ScopeId -ErrorAction SilentlyContinue |
                     Where-Object { $_.ClientId -eq $ClientId } |
                     Select-Object -First 1
            if ($match) { return $match }
        }
        return $null
    }

    function Write-ApiError {
        param([string]$Message, [int]$StatusCode = 500)
        New-PSUApiResponse -StatusCode $StatusCode `
            -Body (@{ error = $Message } | ConvertTo-Json -Compress) `
            -ContentType 'application/json'
    }

}


# ===========================================================================
# SECTION 1 — SCOPES
# ===========================================================================

# ---------------------------------------------------------------------------
# GET /api/dhcp/scopes
# Returns all DHCP scopes on this server, including router (Option 3) and
# the name of any associated failover relationship.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/scopes' -Method GET -Endpoint {
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        $scopes = Get-DhcpServerv4Scope -ErrorAction Stop

        # Build a map: scope_id -> failover_name so we can attach it without
        # an extra cmdlet call per scope.
        $scopeFailoverMap = @{}
        try {
            $allFailovers = Get-DhcpServerv4Failover -ErrorAction SilentlyContinue
            foreach ($fo in $allFailovers) {
                foreach ($sid in $fo.ScopeId) {
                    $scopeFailoverMap[$sid.ToString()] = $fo.Name
                }
            }
        } catch { }

        $result = @(
            $scopes | ForEach-Object {
                $obj = ConvertTo-ScopeObject $_

                # Attach router IP from Option 3 (if configured)
                $routerOpt = Get-DhcpServerv4OptionValue -ScopeId $_.ScopeId -OptionId 3 `
                                 -ErrorAction SilentlyContinue
                $obj['router'] = if ($routerOpt -and $routerOpt.Value) {
                    $routerOpt.Value[0]
                } else { $null }

                # Attach failover relationship name (if this scope is in a failover)
                $obj['failover_name'] = $scopeFailoverMap[$_.ScopeId.ToString()]

                $obj
            }
        )
        $result | ConvertTo-Json -Depth 4 -Compress
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}


# ---------------------------------------------------------------------------
# GET /api/dhcp/scopes/:scope_id
# Returns a single scope by its network address (e.g. "10.0.1.0").
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/scopes/:scope_id' -Method GET -Endpoint {
    param($scope_id)
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        $scope = Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop
        ConvertTo-ScopeObject $scope | ConvertTo-Json -Depth 4 -Compress
    }
    catch {
        Write-ApiError -Message "Scope '$scope_id' not found." -StatusCode 404
    }
}


# ---------------------------------------------------------------------------
# POST /api/dhcp/scopes
# Creates a new DHCP scope.
#
# Expected body:
#   {
#     "scope_id": "10.0.1.0",
#     "name": "Building A",
#     "start_ip": "10.0.1.10",
#     "end_ip": "10.0.1.254",
#     "subnet_mask": "255.255.255.0",
#     "router": "10.0.1.1",           <- optional; sets DHCP Option 3
#     "lease_duration_seconds": 86400,
#     "description": ""
#   }
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/scopes' -Method POST -Endpoint {
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        $body = $Body | ConvertFrom-Json

        if (-not $body.scope_id -or -not $body.start_ip -or -not $body.end_ip -or -not $body.subnet_mask) {
            New-PSUApiResponse -StatusCode 400 `
                -Body (@{ error = 'scope_id, start_ip, end_ip, and subnet_mask are required.' } | ConvertTo-Json -Compress) `
                -ContentType 'application/json'
            return
        }

        $addParams = @{
            ScopeId     = $body.scope_id
            Name        = $body.name
            StartRange  = $body.start_ip
            EndRange    = $body.end_ip
            SubnetMask  = $body.subnet_mask
            ErrorAction = 'Stop'
        }
        if ($body.description) { $addParams['Description'] = $body.description }

        Add-DhcpServerv4Scope @addParams

        # Set lease duration if provided
        if ($body.lease_duration_seconds -and $body.lease_duration_seconds -gt 0) {
            Set-DhcpServerv4Scope -ScopeId $body.scope_id `
                -LeaseDuration ([TimeSpan]::FromSeconds([int]$body.lease_duration_seconds)) `
                -ErrorAction SilentlyContinue
        }

        # Set router (Option 3) if provided
        if ($body.router) {
            Set-DhcpServerv4OptionValue -ScopeId $body.scope_id `
                -OptionId 3 -Value @($body.router) `
                -ErrorAction SilentlyContinue
        }

        $scope = Get-DhcpServerv4Scope -ScopeId $body.scope_id -ErrorAction Stop
        New-PSUApiResponse -StatusCode 201 `
            -Body (ConvertTo-ScopeObject $scope | ConvertTo-Json -Depth 4 -Compress) `
            -ContentType 'application/json'
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}


# ---------------------------------------------------------------------------
# PUT /api/dhcp/scopes/:scope_id
# Updates an existing DHCP scope.
#
# Accepts the same body shape as POST.  Only provided fields are updated.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/scopes/:scope_id' -Method PUT -Endpoint {
    param($scope_id)
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        # Verify scope exists first
        $null = Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop

        $body = $Body | ConvertFrom-Json

        $setParams = @{
            ScopeId     = $scope_id
            ErrorAction = 'Stop'
        }
        if ($body.PSObject.Properties.Name -contains 'name')        { $setParams['Name']        = $body.name }
        if ($body.PSObject.Properties.Name -contains 'start_ip')    { $setParams['StartRange']  = $body.start_ip }
        if ($body.PSObject.Properties.Name -contains 'end_ip')      { $setParams['EndRange']    = $body.end_ip }
        if ($body.PSObject.Properties.Name -contains 'description') { $setParams['Description'] = $body.description }
        if ($body.PSObject.Properties.Name -contains 'lease_duration_seconds' -and $body.lease_duration_seconds -gt 0) {
            $setParams['LeaseDuration'] = [TimeSpan]::FromSeconds([int]$body.lease_duration_seconds)
        }

        Set-DhcpServerv4Scope @setParams

        # Update router (Option 3) if provided
        if ($body.PSObject.Properties.Name -contains 'router') {
            if ($body.router) {
                Set-DhcpServerv4OptionValue -ScopeId $scope_id `
                    -OptionId 3 -Value @($body.router) `
                    -ErrorAction SilentlyContinue
            }
            else {
                Remove-DhcpServerv4OptionValue -ScopeId $scope_id `
                    -OptionId 3 -ErrorAction SilentlyContinue
            }
        }

        $scope = Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop
        ConvertTo-ScopeObject $scope | ConvertTo-Json -Depth 4 -Compress
    }
    catch [Microsoft.Management.Infrastructure.CimException] {
        Write-ApiError -Message "Scope '$scope_id' not found." -StatusCode 404
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}


# ===========================================================================
# SECTION 2 — LEASES
# ===========================================================================

# ---------------------------------------------------------------------------
# GET /api/dhcp/leases?scope_id=10.0.1.0
# Returns active DHCP leases.  scope_id query parameter is optional.
# Filters to Active and ActiveReservation address states only.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/leases' -Method GET -Endpoint {
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        # $scope_id comes from query string automatically in PSU
        $targetScopes = if ($scope_id) {
            Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop
        }
        else {
            Get-DhcpServerv4Scope -ErrorAction Stop
        }

        $result = @()
        foreach ($scope in $targetScopes) {
            $leases = Get-DhcpServerv4Lease -ScopeId $scope.ScopeId -ErrorAction SilentlyContinue |
                      Where-Object { $_.AddressState -in @('Active', 'ActiveReservation') }
            foreach ($lease in $leases) {
                $result += ConvertTo-LeaseObject $lease
            }
        }
        $result | ConvertTo-Json -Depth 4 -Compress
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}


# ===========================================================================
# SECTION 3 — RESERVATIONS
# ===========================================================================

# ---------------------------------------------------------------------------
# GET /api/dhcp/reservations?scope_id=10.0.1.0
# Returns DHCP reservations.  scope_id query parameter is optional.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/reservations' -Method GET -Endpoint {
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        $targetScopes = if ($scope_id) {
            Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop
        }
        else {
            Get-DhcpServerv4Scope -ErrorAction Stop
        }

        $result = @()
        foreach ($scope in $targetScopes) {
            $reservations = Get-DhcpServerv4Reservation -ScopeId $scope.ScopeId -ErrorAction SilentlyContinue
            foreach ($res in $reservations) {
                $result += ConvertTo-ReservationObject $res
            }
        }
        $result | ConvertTo-Json -Depth 4 -Compress
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}


# ---------------------------------------------------------------------------
# POST /api/dhcp/reservations
# Creates a new DHCP reservation.
#
# Expected body:
#   {
#     "scope_id":   "10.0.1.0",
#     "ip_address": "10.0.1.100",
#     "client_id":  "00-11-22-33-44-55",
#     "name":       "printer-01",
#     "description": "",
#     "type":       "Dhcp"          <- "Dhcp", "Bootp", or "Both"
#   }
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/reservations' -Method POST -Endpoint {
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        $body = $Body | ConvertFrom-Json

        if (-not $body.scope_id -or -not $body.ip_address -or -not $body.client_id) {
            New-PSUApiResponse -StatusCode 400 `
                -Body (@{ error = 'scope_id, ip_address, and client_id are required.' } | ConvertTo-Json -Compress) `
                -ContentType 'application/json'
            return
        }

        # Normalise client_id to Windows DHCP format (aa-bb-cc-dd-ee-ff)
        $clientId = $body.client_id.ToLower() -replace '[^0-9a-f]', '' -replace '(..)(?!$)', '$1-'

        $addParams = @{
            ScopeId     = $body.scope_id
            IPAddress   = $body.ip_address
            ClientId    = $clientId
            ErrorAction = 'Stop'
        }
        if ($body.name)        { $addParams['Name']        = $body.name }
        if ($body.description) { $addParams['Description'] = $body.description }
        if ($body.type)        { $addParams['Type']        = $body.type }

        Add-DhcpServerv4Reservation @addParams

        $reservation = Get-DhcpServerv4Reservation -ScopeId $body.scope_id -ErrorAction Stop |
                       Where-Object { $_.IPAddress -eq $body.ip_address } |
                       Select-Object -First 1

        New-PSUApiResponse -StatusCode 201 `
            -Body (ConvertTo-ReservationObject $reservation | ConvertTo-Json -Depth 4 -Compress) `
            -ContentType 'application/json'
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}


# ---------------------------------------------------------------------------
# PUT /api/dhcp/reservations/:client_id
# Updates an existing reservation identified by client MAC address.
#
# Expected body (all fields optional):
#   {
#     "name":        "printer-01-updated",
#     "description": "Updated description",
#     "type":        "Dhcp"
#   }
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/reservations/:client_id' -Method PUT -Endpoint {
    param($client_id)
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        $reservation = Find-ReservationByClientId -ClientId $client_id
        if (-not $reservation) {
            Write-ApiError -Message "Reservation with client_id '$client_id' not found." -StatusCode 404
            return
        }

        $body = $Body | ConvertFrom-Json

        $setParams = @{
            IPAddress   = $reservation.IPAddress
            ErrorAction = 'Stop'
        }
        if ($body.PSObject.Properties.Name -contains 'name')        { $setParams['Name']        = $body.name }
        if ($body.PSObject.Properties.Name -contains 'description') { $setParams['Description'] = $body.description }
        if ($body.PSObject.Properties.Name -contains 'type')        { $setParams['Type']        = $body.type }

        Set-DhcpServerv4Reservation @setParams

        $updated = Get-DhcpServerv4Reservation -ScopeId $reservation.ScopeId -ErrorAction Stop |
                   Where-Object { $_.IPAddress -eq $reservation.IPAddress } |
                   Select-Object -First 1

        ConvertTo-ReservationObject $updated | ConvertTo-Json -Depth 4 -Compress
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}


# ---------------------------------------------------------------------------
# DELETE /api/dhcp/reservations/:client_id
# Removes a reservation by client MAC address.  Returns 204 No Content.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/reservations/:client_id' -Method DELETE -Endpoint {
    param($client_id)
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        $reservation = Find-ReservationByClientId -ClientId $client_id
        if (-not $reservation) {
            Write-ApiError -Message "Reservation with client_id '$client_id' not found." -StatusCode 404
            return
        }

        Remove-DhcpServerv4Reservation `
            -ScopeId  $reservation.ScopeId `
            -IPAddress $reservation.IPAddress `
            -Force `
            -ErrorAction Stop

        New-PSUApiResponse -StatusCode 204
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}


# ===========================================================================
# SECTION 4 — FAILOVER
# ===========================================================================

# ---------------------------------------------------------------------------
# GET /api/dhcp/failover
# Returns all DHCP failover relationships configured on this server.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/failover' -Method GET -Endpoint {
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        $failovers = Get-DhcpServerv4Failover -ErrorAction Stop
        $result = @(
            $failovers | ForEach-Object { ConvertTo-FailoverObject $_ }
        )
        $result | ConvertTo-Json -Depth 5 -Compress
    }
    catch {
        # No failover relationships exist — return empty array
        if ($_.Exception.Message -match 'No DHCP failover relationship') {
            '[]'
        }
        else {
            Write-ApiError -Message $_.Exception.Message -StatusCode 500
        }
    }
}


# ---------------------------------------------------------------------------
# POST /api/dhcp/failover
# Creates a new DHCP failover relationship.  Must be run on the PRIMARY server.
#
# Expected body:
#   {
#     "name":                      "FAILOVER-BUILDING-A",
#     "secondary_server":          "dhcp02.example.com",   <- partner server; PSU runs on primary
#     "scope_ids":                 ["10.0.1.0", "10.0.2.0"],
#     "mode":                      "LoadBalance",           <- or "HotStandby"
#     "max_client_lead_time":      3600,
#     "max_response_delay":        30,
#     "state_switchover_interval": null,                    <- null = disabled
#     "enable_auth":               false,
#     "shared_secret":             ""
#   }
#
# Note: PSU must be running on the PRIMARY server.  The secondary_server value
#       must be reachable by name/IP from this host.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/failover' -Method POST -Endpoint {
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        $body = $Body | ConvertFrom-Json

        if (-not $body.name -or -not $body.secondary_server -or -not $body.scope_ids) {
            New-PSUApiResponse -StatusCode 400 `
                -Body (@{ error = 'name, secondary_server, and scope_ids are required.' } | ConvertTo-Json -Compress) `
                -ContentType 'application/json'
            return
        }

        $mclt = if ($body.max_client_lead_time) { [int]$body.max_client_lead_time } else { 3600 }
        $mrd  = if ($body.max_response_delay)   { [int]$body.max_response_delay   } else { 30   }

        $addParams = @{
            Name              = $body.name
            PartnerServer     = $body.secondary_server
            ScopeId           = @($body.scope_ids)
            MaxClientLeadTime = [TimeSpan]::FromSeconds($mclt)
            MaxResponseDelay  = [TimeSpan]::FromSeconds($mrd)
            ErrorAction       = 'Stop'
        }

        # Mode
        if ($body.mode) { $addParams['Mode'] = $body.mode }

        # State switchover interval (null/0 = disabled)
        if ($body.state_switchover_interval -and [int]$body.state_switchover_interval -gt 0) {
            $addParams['StateSwitchInterval'] = [TimeSpan]::FromSeconds([int]$body.state_switchover_interval)
        }

        # Authentication
        if ($body.enable_auth -eq $true) {
            $addParams['EnableAuth'] = $true
            if ($body.shared_secret) {
                $addParams['SharedSecret'] = $body.shared_secret
            }
        }

        Add-DhcpServerv4Failover @addParams

        $failover = Get-DhcpServerv4Failover -Name $body.name -ErrorAction Stop
        New-PSUApiResponse -StatusCode 201 `
            -Body (ConvertTo-FailoverObject $failover | ConvertTo-Json -Depth 5 -Compress) `
            -ContentType 'application/json'
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}


# ===========================================================================
# SECTION 5 — OPTIONS
# ===========================================================================

# ---------------------------------------------------------------------------
# GET /api/dhcp/options/server
# Returns all option values set at the server level.
# Each item: { code, name, value (array), type, vendor_class }
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/options/server' -Method GET -Endpoint {
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        $options = Get-DhcpServerv4OptionValue -All -ErrorAction Stop
        $result = @(
            $options | ForEach-Object { ConvertTo-OptionValueObject $_ }
        )
        $result | ConvertTo-Json -Depth 4 -Compress
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}


# ---------------------------------------------------------------------------
# GET /api/dhcp/options/scope/:scope_id
# Returns all option values set on a specific scope.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/options/scope/:scope_id' -Method GET -Endpoint {
    param($scope_id)
    . ([scriptblock]::Create($using:HelperFunctions))

    try {
        # Verify scope exists
        $null = Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop

        $options = Get-DhcpServerv4OptionValue -ScopeId $scope_id -All -ErrorAction Stop
        $result = @(
            $options | ForEach-Object { ConvertTo-OptionValueObject $_ }
        )
        $result | ConvertTo-Json -Depth 4 -Compress
    }
    catch [Microsoft.Management.Infrastructure.CimException] {
        Write-ApiError -Message "Scope '$scope_id' not found." -StatusCode 404
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}
