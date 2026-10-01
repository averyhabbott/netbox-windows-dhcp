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

    PSU v5 runs each endpoint in an isolated runspace.  Shared helper functions
    are stored in the $H string and prepended to every endpoint's scriptblock via
    [scriptblock]::Create() so that each runspace is fully self-contained.
    URL path parameters (:param) and the $Body variable are injected automatically
    by PSU — no param() declaration is required inside endpoint scriptblocks.
#>


# ===========================================================================
# CONFIGURATION
# ===========================================================================

# Set to $true to require a PSU App Token on all endpoints.
# Generate a token in the PSU admin console under Security > App Tokens,
# then paste it into the App Token field on the DHCP Server object in NetBox.
$RequireAuthentication = $true

# DHCPReader tokens can call read (GET) endpoints only.
# DHCPWriter tokens can call all endpoints (GET + write).
# Set $RequireAuthentication = $false to disable role enforcement (not recommended).
$_epRead  = if ($RequireAuthentication) {
    @{ Authentication = $true; Role = @('DHCPReader', 'DHCPWriter') }
} else { @{} }

$_epWrite = if ($RequireAuthentication) {
    @{ Authentication = $true; Role = @('DHCPWriter') }
} else { @{} }


# ===========================================================================
# SHARED HELPERS — embedded into every endpoint via [scriptblock]::Create()
# ===========================================================================

$H = @'
if ($PSVersionTable.PSEdition -eq 'Core') {
    Import-Module DhcpServer -SkipEditionCheck -ErrorAction Stop
} else {
    Import-Module DhcpServer -ErrorAction Stop
}

$PSU_SCRIPT_VERSION = '2.0.0'

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

function ConvertTo-ExclusionRangeObject {
    param([Microsoft.Management.Infrastructure.CimInstance]$Exclusion)
    [ordered]@{
        scope_id = $Exclusion.ScopeId.ToString()
        start_ip = $Exclusion.StartRange.ToString()
        end_ip   = $Exclusion.EndRange.ToString()
    }
}

function Write-ApiError {
    param([string]$Message, [int]$StatusCode = 500)
    New-PSUApiResponse -StatusCode $StatusCode `
        -Body (@{ error = $Message } | ConvertTo-Json -Compress) `
        -ContentType 'application/json'
}

function ConvertTo-ScopeStateParam {
    # The -State value for Add-/Set-DhcpServerv4Scope ('Active' or 'InActive', any
    # capitalisation), or $null when $Value is neither.
    param($Value)
    switch -Regex ([string]$Value) {
        '^(?i)active$'   { return 'Active' }
        '^(?i)inactive$' { return 'InActive' }
    }
    return $null
}

function Write-InvalidScopeState {
    # Writes the 400 for a scope 'state' that is not Active or InActive. Call it as a
    # statement, then return (see Write-InvalidIPv4).
    Write-ApiError -Message "'state' must be Active or InActive." -StatusCode 400
}

function Write-InvalidIPv4 {
    # Writes the 400 for a field that is not a valid IPv4 address. Call it as a statement
    # right after a failed Test-IPv4 check, then return: inside an if condition the response
    # would be captured as the condition's value instead of reaching the client.
    param([string]$FieldName)
    Write-ApiError -Message "'$FieldName' must be a valid IPv4 address." -StatusCode 400
}

function ConvertTo-UInt32Ip {
    # IPv4 address (object or string) -> UInt32, for computing range sizes.
    param($Ip)
    $bytes = ([System.Net.IPAddress]::Parse([string]$Ip)).GetAddressBytes()
    [Array]::Reverse($bytes)
    [System.BitConverter]::ToUInt32($bytes, 0)
}

function Test-IPv4 {
    param([string]$Value)
    $addr = $null
    [System.Net.IPAddress]::TryParse($Value, [ref]$addr) -and $addr.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork
}

function ConvertTo-WindowsClientId {
    # Any MAC / client ID format -> Windows DHCP format (aa-bb-cc-dd-ee-ff).
    param([string]$ClientId)
    $ClientId.ToLower() -replace '[^0-9a-f]', '' -replace '(..)(?!$)', '$1-'
}

function Read-BatchBody {
    # Parses a JSON list request body. Returns @{ Items = @(...); Error = msg }.
    # The caller writes the 400: a response written in here would be captured as
    # this function's output instead of reaching the client.
    param([string]$RawBody)
    if (-not $RawBody -or -not $RawBody.TrimStart().StartsWith('[')) {
        return @{ Items = @(); Error = 'Body must be a JSON list of items.' }
    }
    try {
        return @{ Items = @($RawBody | ConvertFrom-Json); Error = $null }
    }
    catch {
        return @{ Items = @(); Error = 'Body is not valid JSON.' }
    }
}

function Test-ReservationItem {
    # Returns an error message for a malformed batch item, or $null when it's usable.
    param($Item)
    if ($Item -isnot [System.Management.Automation.PSCustomObject]) { return 'Item must be an object.' }
    if (-not $Item.scope_id -or -not $Item.ip_address) { return 'scope_id and ip_address are required.' }
    if (-not (Test-IPv4 $Item.scope_id))   { return "'scope_id' must be a valid IPv4 address." }
    if (-not (Test-IPv4 $Item.ip_address)) { return "'ip_address' must be a valid IPv4 address." }
    return $null
}

function New-BatchResult {
    param($Item, [string]$Status, [string]$Message)
    $result = [ordered]@{
        scope_id   = [string]$Item.scope_id
        ip_address = [string]$Item.ip_address
        status     = $Status
    }
    if ($Message) { $result['error'] = $Message }
    $result
}

function Get-ScopeReservations {
    # Reads each scope's reservations once per batch. Returns @{ Map = @{ ip -> reservation }; Error = msg }.
    param([hashtable]$Cache, [string]$ScopeId)
    if (-not $Cache.ContainsKey($ScopeId)) {
        $entry = @{ Map = @{}; Error = $null }
        try {
            foreach ($r in @(Get-DhcpServerv4Reservation -ScopeId $ScopeId -ErrorAction Stop)) {
                $entry.Map[$r.IPAddress.ToString()] = $r
            }
        }
        catch [Microsoft.Management.Infrastructure.CimException] {
            $entry.Error = "Scope $ScopeId not found."
        }
        $Cache[$ScopeId] = $entry
    }
    $Cache[$ScopeId]
}

function Get-ReservationAt {
    # The reservation at this IP, or $null when there isn't one.
    param([string]$IPAddress)
    try { Get-DhcpServerv4Reservation -IPAddress $IPAddress -ErrorAction Stop }
    catch { $null }
}

function Add-BatchReservationDetails {
    # Re-reads each touched scope once and attaches the reservation as the server now has it
    # to every 'ok' result.
    param($Results)
    $fresh = @{}
    foreach ($r in $Results) {
        if ($r.status -ne 'ok') { continue }
        $entry = Get-ScopeReservations -Cache $fresh -ScopeId $r.scope_id
        $ip = [System.Net.IPAddress]::Parse($r.ip_address).ToString()
        if ($entry.Map.ContainsKey($ip)) {
            $r['reservation'] = ConvertTo-ReservationObject $entry.Map[$ip]
        }
    }
}

function Write-BatchResults {
    param($Results)
    ConvertTo-Json -InputObject ([ordered]@{ results = @($Results) }) -Depth 6 -Compress
}

'@


# ===========================================================================
# SECTION 0 — HEALTH
# ===========================================================================

# ---------------------------------------------------------------------------
# GET /api/dhcp/health
# Read-only health check. Returns script version so the NetBox plugin can
# detect PSU script version mismatches. Accessible to DHCPReader + DHCPWriter.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/health' -Method GET @_epRead -Endpoint ([scriptblock]::Create($H + {
    New-PSUApiResponse -StatusCode 200 `
        -Body (@{ status = 'ok'; version = $PSU_SCRIPT_VERSION } | ConvertTo-Json -Compress) `
        -ContentType 'application/json'
}.ToString()))

# ---------------------------------------------------------------------------
# POST /api/dhcp/health
# Write-access health check. Used by the NetBox plugin to verify that the
# configured token has write (DHCPWriter) permissions. DHCPWriter role only.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/health' -Method POST @_epWrite -Endpoint ([scriptblock]::Create($H + {
    New-PSUApiResponse -StatusCode 200 `
        -Body (@{ status = 'ok' } | ConvertTo-Json -Compress) `
        -ContentType 'application/json'
}.ToString()))

# ---------------------------------------------------------------------------
# GET /api/dhcp/metrics
# Aggregate health snapshot for external monitoring systems. Returns server
# statistics, per-scope utilization, failover state, and database health in a
# single call. Read-only; DHCPReader + DHCPWriter.
#
# The schema is intentionally generic (standard DHCP concepts only, no
# consumer-specific fields). Counters under `server.packets` are cumulative
# since the service started; consumers are expected to derive rates. Each
# section is gathered independently so one failing cmdlet degrades to null
# rather than failing the whole response.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/metrics' -Method GET @_epRead -Endpoint ([scriptblock]::Create($H + {
    try {
        $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')

        # --- Scope inventory (names/state), keyed by scope id ------------------
        $scopeInfo = @{}
        $activeCount = 0
        try {
            foreach ($sc in (Get-DhcpServerv4Scope -ErrorAction Stop)) {
                $scopeInfo[$sc.ScopeId.ToString()] = $sc
                if ($sc.State -eq 'Active') { $activeCount++ }
            }
        } catch {
            Write-Verbose "Could not enumerate scopes: $_"
        }

        # --- Server statistics -------------------------------------------------
        $server = $null
        try {
            $s = Get-DhcpServerv4Statistics -ErrorAction Stop
            $uptime = $null
            if ($s.ServerStartTime) {
                $uptime = [int]((Get-Date) - $s.ServerStartTime).TotalSeconds
            }
            $server = [ordered]@{
                uptime_seconds   = $uptime
                scopes_total     = [int]$s.TotalScopes
                scopes_active    = $activeCount
                addresses_total  = [int]($s.AddressesInUse + $s.AddressesAvailable)
                addresses_in_use = [int]$s.AddressesInUse
                addresses_free   = [int]$s.AddressesAvailable
                percent_in_use   = [math]::Round([double]$s.PercentageInUse, 2)
                packets          = [ordered]@{
                    discovers = [int64]$s.Discovers
                    offers    = [int64]$s.Offers
                    requests  = [int64]$s.Requests
                    acks      = [int64]$s.Acks
                    nacks     = [int64]$s.Naks
                    declines  = [int64]$s.Declines
                    releases  = [int64]$s.Releases
                }
            }
        } catch {
            Write-Verbose "Could not read server statistics: $_"
        }

        # --- Per-scope utilization --------------------------------------------
        # Opt-in, query-gated lease scrapes (off by default -> cheapest call):
        #   ?reservations=true  split ReservedAddress into active/inactive
        #   ?declined=true      count bad/declined (conflict) addresses
        # Both need per-scope lease enumeration, so they stay opt-in. The true
        # total and total reserved are always returned (cheap, no enumeration).
        $wantReservations = ("$reservations").Trim().ToLower() -in @('1', 'true', 'yes', 'on')
        $wantDeclined     = ("$declined").Trim().ToLower()     -in @('1', 'true', 'yes', 'on')

        # True scope size = address range - exclusions (one aggregate call). Unlike
        # InUse+Free it is independent of lease state and doesn't double-count
        # active reservations (which already sit inside AddressesInUse).
        $exclByScope = @{}
        $exclOk = $false
        try {
            foreach ($ex in (Get-DhcpServerv4ExclusionRange -ErrorAction Stop)) {
                $esid = $ex.ScopeId.ToString()
                $n = [int]((ConvertTo-UInt32Ip $ex.EndRange) - (ConvertTo-UInt32Ip $ex.StartRange) + 1)
                if ($exclByScope.ContainsKey($esid)) { $exclByScope[$esid] += $n } else { $exclByScope[$esid] = $n }
            }
            $exclOk = $true
        } catch {
            Write-Verbose "Could not read exclusion ranges: $_"
        }

        $scopes = @()
        try {
            foreach ($st in (Get-DhcpServerv4ScopeStatistics -ErrorAction Stop)) {
                $sid  = $st.ScopeId.ToString()
                $info = $scopeInfo[$sid]
                $inUse = [int]$st.AddressesInUse
                $free  = [int]$st.AddressesFree
                $reserved = [int]$st.ReservedAddress

                # range - exclusions when both are known, else fall back to InUse+Free.
                $total = $inUse + $free
                if ($exclOk -and $info -and $info.StartRange -and $info.EndRange) {
                    $rangeCount = [int]((ConvertTo-UInt32Ip $info.EndRange) - (ConvertTo-UInt32Ip $info.StartRange) + 1)
                    if ($rangeCount -gt 0) { $total = $rangeCount - [int]($exclByScope[$sid]) }
                }

                # Gated enumeration. With reservation monitoring on and reservations
                # present, one -AllLeases pass yields the active/inactive split and
                # (when declined is also on) the Declined count for free.
                $resActive = 0; $resInactive = 0; $badCount = 0; $enumerated = $false
                if ($wantReservations -and $reserved -gt 0) {
                    try {
                        $byState = Get-DhcpServerv4Lease -ScopeId $sid -AllLeases -ErrorAction Stop | Group-Object AddressState
                        foreach ($g in $byState) {
                            switch ($g.Name) {
                                'ActiveReservation'   { $resActive   = [int]$g.Count }
                                'InactiveReservation' { $resInactive = [int]$g.Count }
                                'Declined'            { if ($wantDeclined) { $badCount = [int]$g.Count } }
                            }
                        }
                        $enumerated = $true
                    } catch {
                        Write-Verbose "Could not enumerate leases for ${sid}: $_"
                    }
                }
                # Scopes not enumerated above still need a (server-side filtered)
                # bad-lease count when declined monitoring is on.
                if ($wantDeclined -and -not $enumerated) {
                    try {
                        $badCount = @(Get-DhcpServerv4Lease -ScopeId $sid -BadLeases -ErrorAction Stop).Count
                    } catch {
                        Write-Verbose "Could not read bad leases for ${sid}: $_"
                    }
                }

                $scopes += [ordered]@{
                    scope_id              = $sid
                    name                  = if ($info) { [string]$info.Name } else { $null }
                    state                 = if ($info) { $info.State.ToString().ToLower() } else { $null }
                    addresses_total       = [int]$total
                    addresses_in_use      = $inUse
                    addresses_free        = $free
                    addresses_reserved    = $reserved
                    reservations_active   = $resActive
                    reservations_inactive = $resInactive
                    pending_offers        = [int]$st.PendingOffers
                    bad_address_count     = $badCount
                    percent_in_use        = [math]::Round([double]$st.PercentageInUse, 2)
                }
            }
        } catch {
            Write-Verbose "Could not read scope statistics: $_"
        }

        # --- Failover relationships -------------------------------------------
        # `state` reflects the live relationship state when the OS exposes it.
        # `in_sync` is left null unless the OS reports a definitive value, since
        # it cannot be reliably derived from a single server's view.
        $failover = @()
        try {
            foreach ($fo in (Get-DhcpServerv4Failover -ErrorAction Stop)) {
                $state = $null
                if ($fo.PSObject.Properties['State']) { $state = ([string]$fo.State).ToLower() }
                $failover += [ordered]@{
                    name           = [string]$fo.Name
                    mode           = $fo.Mode.ToString()
                    partner_server = [string]$fo.PartnerServer
                    state          = $state
                    in_sync        = $null
                }
            }
        } catch {
            Write-Verbose "Could not read failover relationships: $_"
        }

        # --- Database / backup health -----------------------------------------
        $database = $null
        try {
            $db = Get-DhcpServerDatabase -ErrorAction Stop
            $database = [ordered]@{
                backup_interval_minutes = [int]$db.BackupInterval.TotalMinutes
                logging_enabled         = [bool]$db.LoggingEnabled
                cleanup_interval_minutes = [int]$db.CleanupInterval.TotalMinutes
            }
        } catch {
            Write-Verbose "Could not read database settings: $_"
        }

        $payload = [ordered]@{
            schema_version = 1
            generated_at   = $now
            server         = $server
            scopes         = $scopes
            failover       = $failover
            database       = $database
        }

        New-PSUApiResponse -StatusCode 200 `
            -Body ($payload | ConvertTo-Json -Depth 5 -Compress) `
            -ContentType 'application/json'
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ===========================================================================
# SECTION 1 — SCOPES
# ===========================================================================

# ---------------------------------------------------------------------------
# GET /api/dhcp/scopes
# Returns all DHCP scopes on this server, including router (Option 3) and
# the name of any associated failover relationship.
#
# Optional query parameter:
#   include_router=false  <- leave out "router" (saves one call per scope);
#                            the caller reads Option 3 from the options data
# A failed failover or router read fails the whole request, so a Windows
# error is never mistaken for "no failover" or "no router".
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/scopes' -Method GET @_epRead -Endpoint ([scriptblock]::Create($H + {
    try {
        $scopes = Get-DhcpServerv4Scope -ErrorAction Stop
        if ($active_only -eq 'true') {
            $scopes = $scopes | Where-Object { $_.State -eq 'Active' }
        }

        # Build a map: scope_id -> failover_name so we can attach it without
        # an extra cmdlet call per scope.
        $scopeFailoverMap = @{}
        $allFailovers = Get-DhcpServerv4Failover -ErrorAction Stop
        foreach ($fo in @($allFailovers)) {
            foreach ($sid in $fo.ScopeId) {
                $scopeFailoverMap[$sid.ToString()] = $fo.Name
            }
        }

        $result = @(
            $scopes | ForEach-Object {
                $obj = ConvertTo-ScopeObject $_

                # Attach router IP from Option 3 (if configured). Read with -All:
                # asking for an unset Option 3 on its own is an error.
                if ($include_router -ne 'false') {
                    $routerOpt = Get-DhcpServerv4OptionValue -ScopeId $_.ScopeId -All -ErrorAction Stop |
                                     Where-Object { $_.OptionId -eq 3 -and -not $_.VendorClass } |
                                     Select-Object -First 1
                    $obj['router'] = if ($routerOpt -and $routerOpt.Value) {
                        $routerOpt.Value[0]
                    } else { $null }
                }

                # Attach failover relationship name (if this scope is in a failover)
                $obj['failover_name'] = $scopeFailoverMap[$_.ScopeId.ToString()]

                $obj
            }
        )
        ConvertTo-Json -InputObject $result -Depth 4 -Compress
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ---------------------------------------------------------------------------
# GET /api/dhcp/scopes/:scope_id
# Returns a single scope by its network address (e.g. "10.0.1.0").
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/scopes/:scope_id' -Method GET @_epRead -Endpoint ([scriptblock]::Create($H + {
    if (-not (Test-IPv4 $scope_id)) { Write-InvalidIPv4 'scope_id'; return }
    try {
        $scope = Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop
        ConvertTo-ScopeObject $scope | ConvertTo-Json -Depth 4 -Compress
    }
    catch [Microsoft.Management.Infrastructure.CimException] {
        Write-ApiError -Message 'Scope not found.' -StatusCode 404
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


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
#     "description": "",
#     "state": "Active",              <- optional; Active or InActive (default Active)
#     "options": {                    <- optional; arbitrary option codes
#       "set": [{"code": 6, "value": ["10.0.0.1", "10.0.0.2"]}]
#     },
#     "failover": { "enroll": "FAILOVER-BUILDING-A" }   <- optional; relationship
#                                                           must already exist
#   }
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/scopes' -Method POST @_epWrite -Endpoint ([scriptblock]::Create($H + {
    try {
        $body = $Body | ConvertFrom-Json

        if (-not $body.scope_id -or -not $body.start_ip -or -not $body.end_ip -or -not $body.subnet_mask) {
            New-PSUApiResponse -StatusCode 400 `
                -Body (@{ error = 'scope_id, start_ip, end_ip, and subnet_mask are required.' } | ConvertTo-Json -Compress) `
                -ContentType 'application/json'
            return
        }
        if (-not (Test-IPv4 $body.scope_id))   { Write-InvalidIPv4 'scope_id'; return }
        if (-not (Test-IPv4 $body.start_ip))   { Write-InvalidIPv4 'start_ip'; return }
        if (-not (Test-IPv4 $body.end_ip))     { Write-InvalidIPv4 'end_ip'; return }
        if (-not (Test-IPv4 $body.subnet_mask)) { Write-InvalidIPv4 'subnet_mask'; return }
        if ($body.router -and $body.router -ne '' -and -not (Test-IPv4 $body.router)) { Write-InvalidIPv4 'router'; return }
        $state = $null
        if ($body.state) {
            $state = ConvertTo-ScopeStateParam $body.state
            if (-not $state) { Write-InvalidScopeState; return }
        }

        $addParams = @{
            Name        = $body.name
            StartRange  = $body.start_ip
            EndRange    = $body.end_ip
            SubnetMask  = $body.subnet_mask
            ErrorAction = 'Stop'
        }
        if ($body.description) { $addParams['Description'] = $body.description }
        if ($state) { $addParams['State'] = $state }

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

        # Set additional option values, if provided (excludes Option 3/51, which
        # are handled via the router/lease_duration_seconds fields above).
        # -Force skips Set-DhcpServerv4OptionValue's built-in validation (e.g. a
        # DNS-server reachability check for Option 6) — NetBox is authoritative
        # here, so a value it has decided to push should be applied as given
        # rather than silently rejected because the DHCP server can't reach it.
        if ($body.options -and $body.options.set) {
            foreach ($opt in $body.options.set) {
                Set-DhcpServerv4OptionValue -ScopeId $body.scope_id `
                    -OptionId ([int]$opt.code) -Value @($opt.value) `
                    -Force -ErrorAction SilentlyContinue
            }
        }

        # Enroll in a failover relationship, if requested. The relationship
        # must already exist on this server (created separately via
        # POST /api/dhcp/failover) — Add-DhcpServerv4FailoverScope adds a scope
        # to an existing relationship by name; Add-DhcpServerv4Failover is only
        # for creating a brand new relationship (always requires -PartnerServer).
        if ($body.failover -and $body.failover.enroll) {
            Add-DhcpServerv4FailoverScope -ScopeId $body.scope_id -Name $body.failover.enroll -ErrorAction Stop
        }

        $scope = Get-DhcpServerv4Scope -ScopeId $body.scope_id -ErrorAction Stop
        New-PSUApiResponse -StatusCode 201 `
            -Body (ConvertTo-ScopeObject $scope | ConvertTo-Json -Depth 4 -Compress) `
            -ContentType 'application/json'
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ---------------------------------------------------------------------------
# PUT /api/dhcp/scopes/:scope_id
# Updates an existing DHCP scope.
#
# Accepts the same body shape as POST.  Only provided fields are updated
# ("state" switches the scope Active/InActive).
# "options" and "failover", if provided, are applied as explicit instructions
# rather than a desired-state list — the caller (NetBox) has already diffed
# against the live scope, so this endpoint just executes them verbatim:
#   "options": {
#     "set":    [{"code": 6, "value": ["10.0.0.1", "10.0.0.2"]}],
#     "remove": [66]
#   },
#   "failover": {
#     "remove": "FAILOVER-CURRENT-NAME",  <- pull out of this (current) relationship;
#                                             must be the exact relationship the scope
#                                             is presently a member of
#     "enroll": "FAILOVER-BUILDING-A"     <- then add to this one (reassignment
#                                             sends both; the relationship must
#                                             already exist)
#   }
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/scopes/:scope_id' -Method PUT @_epWrite -Endpoint ([scriptblock]::Create($H + {
    if (-not (Test-IPv4 $scope_id)) { Write-InvalidIPv4 'scope_id'; return }

    # Verify scope exists first. This check gets its own try/catch — kept separate
    # from the update logic below so a real failure there (e.g. Set-DhcpServerv4OptionValue
    # rejecting a value) can't be misreported as "Scope not found" just because it
    # also happens to throw a CimException.
    try {
        $null = Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop
    }
    catch [Microsoft.Management.Infrastructure.CimException] {
        Write-ApiError -Message 'Scope not found.' -StatusCode 404
        return
    }

    try {
        $body = $Body | ConvertFrom-Json
        if ($body.PSObject.Properties.Name -contains 'start_ip'  -and $body.start_ip  -and -not (Test-IPv4 $body.start_ip))  { Write-InvalidIPv4 'start_ip'; return }
        if ($body.PSObject.Properties.Name -contains 'end_ip'    -and $body.end_ip    -and -not (Test-IPv4 $body.end_ip))    { Write-InvalidIPv4 'end_ip'; return }
        if ($body.PSObject.Properties.Name -contains 'router'    -and $body.router    -and -not (Test-IPv4 $body.router))    { Write-InvalidIPv4 'router'; return }
        $state = $null
        if ($body.PSObject.Properties.Name -contains 'state' -and $body.state) {
            $state = ConvertTo-ScopeStateParam $body.state
            if (-not $state) { Write-InvalidScopeState; return }
        }

        $setParams = @{
            ScopeId     = $scope_id
            ErrorAction = 'Stop'
        }
        $hasScopeChanges = $false
        if ($body.PSObject.Properties.Name -contains 'name')        { $setParams['Name']        = $body.name; $hasScopeChanges = $true }
        if ($body.PSObject.Properties.Name -contains 'start_ip')    { $setParams['StartRange']  = $body.start_ip; $hasScopeChanges = $true }
        if ($body.PSObject.Properties.Name -contains 'end_ip')      { $setParams['EndRange']    = $body.end_ip; $hasScopeChanges = $true }
        if ($body.PSObject.Properties.Name -contains 'description') { $setParams['Description'] = $body.description; $hasScopeChanges = $true }
        if ($state)                                                 { $setParams['State']       = $state; $hasScopeChanges = $true }
        if ($body.PSObject.Properties.Name -contains 'lease_duration_seconds' -and $body.lease_duration_seconds -gt 0) {
            $setParams['LeaseDuration'] = [TimeSpan]::FromSeconds([int]$body.lease_duration_seconds)
            $hasScopeChanges = $true
        }

        # Set-DhcpServerv4Scope rejects being called with no optional parameters
        # (WIN32 87) — skip it entirely for a body that only carries options/
        # router/failover instructions and no scope-attribute changes.
        if ($hasScopeChanges) {
            Set-DhcpServerv4Scope @setParams
        }

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

        # Apply option value changes, if provided (excludes Option 3/51, which
        # are handled via the router/lease_duration_seconds fields above).
        # -Force skips Set-DhcpServerv4OptionValue's built-in validation (e.g. a
        # DNS-server reachability check for Option 6) — NetBox is authoritative
        # here, so a value it has decided to push should be applied as given
        # rather than silently rejected because the DHCP server can't reach it.
        if ($body.options) {
            foreach ($opt in $body.options.set) {
                Set-DhcpServerv4OptionValue -ScopeId $scope_id `
                    -OptionId ([int]$opt.code) -Value @($opt.value) `
                    -Force -ErrorAction SilentlyContinue
            }
            foreach ($code in $body.options.remove) {
                Remove-DhcpServerv4OptionValue -ScopeId $scope_id `
                    -OptionId ([int]$code) -ErrorAction SilentlyContinue
            }
        }

        # Reconcile failover relationship membership, if requested. Applied in
        # remove-then-enroll order so a reassignment (both keys present) lands
        # the scope in its new relationship rather than erroring because it's
        # still a member of the old one.
        if ($body.failover) {
            if ($body.failover.remove) {
                # Remove-DhcpServerv4Failover has no -ScopeId parameter — it deletes
                # an entire relationship from both partners. Removing a single scope
                # from a relationship (without touching the relationship itself)
                # requires Remove-DhcpServerv4FailoverScope, which also deletes the
                # scope from the partner server as part of deconfiguring it.
                Remove-DhcpServerv4FailoverScope -Name $body.failover.remove -ScopeId $scope_id -Force -ErrorAction Stop
            }
            if ($body.failover.enroll) {
                Add-DhcpServerv4FailoverScope -ScopeId $scope_id -Name $body.failover.enroll -ErrorAction Stop
            }
        }

        $scope = Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop
        ConvertTo-ScopeObject $scope | ConvertTo-Json -Depth 4 -Compress
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ---------------------------------------------------------------------------
# DELETE /api/dhcp/scopes/:scope_id
# Deletes a DHCP scope. Returns 204 No Content.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/scopes/:scope_id' -Method DELETE @_epWrite -Endpoint ([scriptblock]::Create($H + {
    if (-not (Test-IPv4 $scope_id)) { Write-InvalidIPv4 'scope_id'; return }
    try {
        Remove-DhcpServerv4Scope -ScopeId $scope_id -Force -ErrorAction Stop
        New-PSUApiResponse -StatusCode 204
    }
    catch [Microsoft.Management.Infrastructure.CimException] {
        Write-ApiError -Message 'Scope not found.' -StatusCode 404
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ===========================================================================
# SECTION 2 — LEASES
# ===========================================================================

# ---------------------------------------------------------------------------
# GET /api/dhcp/leases?scope_id=10.0.1.0
# Returns active DHCP leases.  scope_id query parameter is optional.
# Filters to Active and ActiveReservation address states only.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/leases' -Method GET @_epRead -Endpoint ([scriptblock]::Create($H + {
    try {
        # $scope_id comes from the query string automatically in PSU
        if ($scope_id -and -not (Test-IPv4 $scope_id)) { Write-InvalidIPv4 'scope_id'; return }
        $targetScopes = if ($scope_id) {
            Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop
        }
        else {
            Get-DhcpServerv4Scope -ErrorAction Stop
        }

        if ($format -eq 'grouped') {
            # Grouped dict keyed by scope_id — opt-in for plugin 1.4.0+ bulk fetch path.
            $result = [ordered]@{}
            foreach ($scope in $targetScopes) {
                $sid = $scope.ScopeId.ToString()
                $leases = Get-DhcpServerv4Lease -ScopeId $sid -ErrorAction Stop |
                          Where-Object { $_.AddressState -in @('Active', 'ActiveReservation') }
                $result[$sid] = @(
                    $leases | ForEach-Object { ConvertTo-LeaseObject $_ }
                )
            }
        } else {
            # Flat list — default (backward-compatible with plugin < 1.4.0).
            $result = @()
            foreach ($scope in $targetScopes) {
                $leases = Get-DhcpServerv4Lease -ScopeId $scope.ScopeId -ErrorAction Stop |
                          Where-Object { $_.AddressState -in @('Active', 'ActiveReservation') }
                foreach ($lease in $leases) {
                    $result += ConvertTo-LeaseObject $lease
                }
            }
        }
        ConvertTo-Json -InputObject $result -Depth 4 -Compress
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ===========================================================================
# SECTION 3 — RESERVATIONS
# ===========================================================================

# ---------------------------------------------------------------------------
# GET /api/dhcp/reservations?scope_id=10.0.1.0
# Returns DHCP reservations.  scope_id query parameter is optional.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/reservations' -Method GET @_epRead -Endpoint ([scriptblock]::Create($H + {
    try {
        if ($scope_id -and -not (Test-IPv4 $scope_id)) { Write-InvalidIPv4 'scope_id'; return }
        $targetScopes = if ($scope_id) {
            Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop
        }
        else {
            Get-DhcpServerv4Scope -ErrorAction Stop
        }

        if ($format -eq 'grouped') {
            # Grouped dict keyed by scope_id — opt-in for plugin 1.4.0+ bulk fetch path.
            $result = [ordered]@{}
            foreach ($scope in $targetScopes) {
                $sid = $scope.ScopeId.ToString()
                $reservations = Get-DhcpServerv4Reservation -ScopeId $sid -ErrorAction Stop
                $result[$sid] = @(
                    $reservations | ForEach-Object { ConvertTo-ReservationObject $_ }
                )
            }
        } else {
            # Flat list — default (backward-compatible with plugin < 1.4.0).
            $result = @()
            foreach ($scope in $targetScopes) {
                $reservations = Get-DhcpServerv4Reservation -ScopeId $scope.ScopeId -ErrorAction Stop
                foreach ($res in $reservations) {
                    $result += ConvertTo-ReservationObject $res
                }
            }
        }
        ConvertTo-Json -InputObject $result -Depth 4 -Compress
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


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
#
# Or a list of those objects (bulk create). Each item is created on its own and
# the response is 200 with one result per item, in order:
#   { "results": [ { "scope_id", "ip_address", "status": "ok" | "error",
#                    "error"?, "reservation"? }, ... ] }
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/reservations' -Method POST @_epWrite -Endpoint ([scriptblock]::Create($H + {
    try {
        if ($Body -and $Body.TrimStart().StartsWith('[')) {
            $batch = Read-BatchBody $Body
            if ($batch.Error) { Write-ApiError -Message $batch.Error -StatusCode 400; return }
            $items = $batch.Items
            $results = [System.Collections.Generic.List[object]]::new()
            foreach ($item in $items) {
                $problem = Test-ReservationItem $item
                if (-not $problem -and -not $item.client_id) { $problem = 'client_id is required.' }
                if ($problem) { $results.Add((New-BatchResult $item 'error' $problem)); continue }
                try {
                    $addParams = @{
                        ScopeId     = $item.scope_id
                        IPAddress   = $item.ip_address
                        ClientId    = ConvertTo-WindowsClientId $item.client_id
                        ErrorAction = 'Stop'
                    }
                    if ($item.name)        { $addParams['Name']        = $item.name }
                    if ($item.description) { $addParams['Description'] = $item.description }
                    if ($item.type)        { $addParams['Type']        = $item.type }
                    Add-DhcpServerv4Reservation @addParams
                    $results.Add((New-BatchResult $item 'ok'))
                }
                catch {
                    $results.Add((New-BatchResult $item 'error' $_.Exception.Message))
                }
            }
            Add-BatchReservationDetails $results
            Write-BatchResults $results
            return
        }

        $body = $Body | ConvertFrom-Json

        if (-not $body.scope_id -or -not $body.ip_address -or -not $body.client_id) {
            New-PSUApiResponse -StatusCode 400 `
                -Body (@{ error = 'scope_id, ip_address, and client_id are required.' } | ConvertTo-Json -Compress) `
                -ContentType 'application/json'
            return
        }
        if (-not (Test-IPv4 $body.scope_id))   { Write-InvalidIPv4 'scope_id'; return }
        if (-not (Test-IPv4 $body.ip_address)) { Write-InvalidIPv4 'ip_address'; return }

        $clientId = ConvertTo-WindowsClientId $body.client_id

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
}.ToString()))


# ---------------------------------------------------------------------------
# PUT /api/dhcp/reservations
# Bulk update. Each reservation is found by scope_id + ip_address. Only the
# optional keys sent are changed; a key whose value already matches is skipped.
#
# Expected body (a list):
#   [
#     { "scope_id": "10.0.1.0", "ip_address": "10.0.1.100",
#       "client_id": "00-11-22-33-44-55", "name": "printer-01",
#       "description": "", "type": "Both" }
#   ]
#
# Returns 200 with one result per item, in order:
#   { "results": [ { "scope_id", "ip_address",
#                    "status": "ok" | "not_found" | "error",
#                    "error"?, "reservation"? }, ... ] }
# 'not_found' means the scope has no reservation at that IP (or the scope
# doesn't exist). A body that isn't a JSON list is a 400.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/reservations' -Method PUT @_epWrite -Endpoint ([scriptblock]::Create($H + {
    try {
        $batch = Read-BatchBody $Body
        if ($batch.Error) { Write-ApiError -Message $batch.Error -StatusCode 400; return }
        $items = $batch.Items
        $cache = @{}
        $results = [System.Collections.Generic.List[object]]::new()
        foreach ($item in $items) {
            $problem = Test-ReservationItem $item
            if ($problem) { $results.Add((New-BatchResult $item 'error' $problem)); continue }
            try {
                $entry = Get-ScopeReservations -Cache $cache -ScopeId $item.scope_id
                if ($entry.Error) { $results.Add((New-BatchResult $item 'not_found' $entry.Error)); continue }
                $ip = [System.Net.IPAddress]::Parse($item.ip_address).ToString()
                $current = $entry.Map[$ip]
                if (-not $current) {
                    $results.Add((New-BatchResult $item 'not_found' "No reservation at $ip in scope $($item.scope_id)."))
                    continue
                }

                $keys = $item.PSObject.Properties.Name
                $setParams = @{ IPAddress = $ip; ErrorAction = 'Stop' }
                if ($keys -contains 'client_id') {
                    $clientId = ConvertTo-WindowsClientId $item.client_id
                    if ($clientId -ne [string]$current.ClientId) {
                        # Windows changes a ClientId by removing and re-adding the reservation.
                        # If the new ClientId is taken, the re-add fails and the reservation is
                        # lost, so refuse up front instead of calling Windows.
                        $holder = $entry.Map.Values | Where-Object { [string]$_.ClientId -eq $clientId } | Select-Object -First 1
                        if ($holder) {
                            $results.Add((New-BatchResult $item 'error' "client_id $clientId is already used by $($holder.IPAddress) in scope $($item.scope_id)."))
                            continue
                        }
                        $setParams['ClientId'] = $clientId
                    }
                }
                if ($keys -contains 'name' -and [string]$item.name -cne [string]$current.Name) {
                    $setParams['Name'] = [string]$item.name
                }
                if ($keys -contains 'description' -and [string]$item.description -cne [string]$current.Description) {
                    $setParams['Description'] = [string]$item.description
                }
                if ($keys -contains 'type' -and [string]$item.type -ne $current.Type.ToString()) {
                    $setParams['Type'] = [string]$item.type
                }

                # Set-DhcpServerv4Reservation rejects being called with no optional
                # parameters (WIN32 87), so skip it when nothing actually changes.
                if ($setParams.Count -gt 2) {
                    try {
                        Set-DhcpServerv4Reservation @setParams
                    }
                    catch {
                        $message = $_.Exception.Message
                        if ($setParams.ContainsKey('ClientId') -and -not (Get-ReservationAt $ip)) {
                            # The failed ClientId change took the reservation with it: put the original back.
                            $restore = @{
                                ScopeId     = $item.scope_id
                                IPAddress   = $ip
                                ClientId    = [string]$current.ClientId
                                Type        = $current.Type.ToString()
                                ErrorAction = 'Stop'
                            }
                            if ($current.Name)        { $restore['Name']        = [string]$current.Name }
                            if ($current.Description) { $restore['Description'] = [string]$current.Description }
                            try {
                                Add-DhcpServerv4Reservation @restore
                                $message += ' The original reservation was restored.'
                            }
                            catch {
                                $entry.Map.Remove($ip)
                                $message += " The original reservation could not be restored: $($_.Exception.Message)"
                            }
                        }
                        $results.Add((New-BatchResult $item 'error' $message))
                        continue
                    }
                    # Keep the scope's map current, so a later item in this batch sees the new ClientId.
                    $entry.Map[$ip] = Get-ReservationAt $ip
                }
                $results.Add((New-BatchResult $item 'ok'))
            }
            catch {
                $results.Add((New-BatchResult $item 'error' $_.Exception.Message))
            }
        }
        Add-BatchReservationDetails $results
        Write-BatchResults $results
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ---------------------------------------------------------------------------
# DELETE /api/dhcp/reservations
# Bulk delete. Each reservation is found by scope_id + ip_address.
#
# Expected body (a list):
#   [ { "scope_id": "10.0.1.0", "ip_address": "10.0.1.100" } ]
#
# Returns 200 with one result per item, in order ("ok" | "not_found" | "error"),
# in the same shape as PUT. A body that isn't a JSON list is a 400.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/reservations' -Method DELETE @_epWrite -Endpoint ([scriptblock]::Create($H + {
    try {
        $batch = Read-BatchBody $Body
        if ($batch.Error) { Write-ApiError -Message $batch.Error -StatusCode 400; return }
        $items = $batch.Items
        $cache = @{}
        $results = [System.Collections.Generic.List[object]]::new()
        foreach ($item in $items) {
            $problem = Test-ReservationItem $item
            if ($problem) { $results.Add((New-BatchResult $item 'error' $problem)); continue }
            try {
                $entry = Get-ScopeReservations -Cache $cache -ScopeId $item.scope_id
                if ($entry.Error) { $results.Add((New-BatchResult $item 'not_found' $entry.Error)); continue }
                $ip = [System.Net.IPAddress]::Parse($item.ip_address).ToString()
                if (-not $entry.Map.ContainsKey($ip)) {
                    $results.Add((New-BatchResult $item 'not_found' "No reservation at $ip in scope $($item.scope_id)."))
                    continue
                }
                # The scope check above is the safety net: by IP alone, Windows would
                # remove the reservation from whichever scope holds that IP.
                Remove-DhcpServerv4Reservation -IPAddress $ip -Confirm:$false -ErrorAction Stop
                $entry.Map.Remove($ip)
                $results.Add((New-BatchResult $item 'ok'))
            }
            catch {
                $results.Add((New-BatchResult $item 'error' $_.Exception.Message))
            }
        }
        Write-BatchResults $results
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ===========================================================================
# SECTION 4 — FAILOVER
# ===========================================================================

# ---------------------------------------------------------------------------
# GET /api/dhcp/failover
# Returns all DHCP failover relationships configured on this server.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/failover' -Method GET @_epRead -Endpoint ([scriptblock]::Create($H + {
    try {
        $failovers = Get-DhcpServerv4Failover -ErrorAction Stop
        $result = @(
            $failovers | ForEach-Object { ConvertTo-FailoverObject $_ }
        )
        ConvertTo-Json -InputObject $result -Depth 5 -Compress
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
}.ToString()))


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
New-PSUEndpoint -Url '/api/dhcp/failover' -Method POST @_epWrite -Endpoint ([scriptblock]::Create($H + {
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
        if ($body.mode) { $addParams['Mode'] = $body.mode }
        if ($body.state_switchover_interval -and [int]$body.state_switchover_interval -gt 0) {
            $addParams['StateSwitchInterval'] = [TimeSpan]::FromSeconds([int]$body.state_switchover_interval)
        }
        if ($body.enable_auth -eq $true) {
            $addParams['EnableAuth'] = $true
            if ($body.shared_secret) { $addParams['SharedSecret'] = $body.shared_secret }
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
}.ToString()))


# ---------------------------------------------------------------------------
# POST /api/dhcp/failover/replicate
# Forces failover replication for specific scopes only — much faster than
# replicating an entire relationship (Invoke-DhcpServerv4FailoverReplication
# with -Name walks every scope the relationship covers). Each scope's
# relationship/partner is resolved internally by the cmdlet, so scope IDs
# from different relationships can be batched into a single call.
#
# Expected body:
#   { "scope_ids": ["10.101.1.0", "10.101.2.0"] }
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/failover/replicate' -Method POST @_epWrite -Endpoint ([scriptblock]::Create($H + {
    try {
        $body = $Body | ConvertFrom-Json

        if (-not $body.scope_ids -or @($body.scope_ids).Count -eq 0) {
            New-PSUApiResponse -StatusCode 400 `
                -Body (@{ error = 'scope_ids is required and must be a non-empty array.' } | ConvertTo-Json -Compress) `
                -ContentType 'application/json'
            return
        }
        foreach ($sid in @($body.scope_ids)) {
            if (-not (Test-IPv4 $sid)) { Write-InvalidIPv4 'scope_ids'; return }
        }

        Invoke-DhcpServerv4FailoverReplication -ScopeId @($body.scope_ids) -Force -ErrorAction Stop

        New-PSUApiResponse -StatusCode 200 `
            -Body (@{ replicated = @($body.scope_ids) } | ConvertTo-Json -Compress) `
            -ContentType 'application/json'
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ===========================================================================
# SECTION 5 — OPTIONS
# ===========================================================================

# ---------------------------------------------------------------------------
# GET /api/dhcp/options
# Bulk fetch all scope-level option values in a single call.
# Returns an extensible envelope:
#   {
#     "scope_options": { scope_id: [option_obj, ...] },
#     "server_options": [],         <- placeholder for a future version
#     "option_definitions": []      <- placeholder for a future version
#   }
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/options' -Method GET @_epRead -Endpoint ([scriptblock]::Create($H + {
    try {
        $scopes = Get-DhcpServerv4Scope -ErrorAction Stop
        $scopeOptions = [ordered]@{}
        foreach ($scope in $scopes) {
            $sid = $scope.ScopeId.ToString()
            $options = Get-DhcpServerv4OptionValue -ScopeId $sid -All -ErrorAction Stop
            $scopeOptions[$sid] = @(
                $options | ForEach-Object { ConvertTo-OptionValueObject $_ }
            )
        }
        $result = [ordered]@{
            scope_options      = $scopeOptions
            server_options     = @()
            option_definitions = @()
        }
        ConvertTo-Json -InputObject $result -Depth 5 -Compress
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ---------------------------------------------------------------------------
# GET /api/dhcp/options/server
# Returns all option values set at the server level.
# Each item: { code, name, value (array), type, vendor_class }
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/options/server' -Method GET @_epRead -Endpoint ([scriptblock]::Create($H + {
    try {
        $options = Get-DhcpServerv4OptionValue -All -ErrorAction Stop
        $result = @(
            $options | ForEach-Object { ConvertTo-OptionValueObject $_ }
        )
        ConvertTo-Json -InputObject $result -Depth 4 -Compress
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ---------------------------------------------------------------------------
# GET /api/dhcp/options/scope/:scope_id
# Returns all option values set on a specific scope.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/options/scope/:scope_id' -Method GET @_epRead -Endpoint ([scriptblock]::Create($H + {
    if (-not (Test-IPv4 $scope_id)) { Write-InvalidIPv4 'scope_id'; return }
    try {
        # Verify scope exists
        $null = Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop

        $options = Get-DhcpServerv4OptionValue -ScopeId $scope_id -All -ErrorAction Stop
        $result = @(
            $options | ForEach-Object { ConvertTo-OptionValueObject $_ }
        )
        ConvertTo-Json -InputObject $result -Depth 4 -Compress
    }
    catch [Microsoft.Management.Infrastructure.CimException] {
        Write-ApiError -Message 'Scope not found.' -StatusCode 404
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ===========================================================================
# SECTION 6 — EXCLUSION RANGES
# ===========================================================================

# ---------------------------------------------------------------------------
# GET /api/dhcp/exclusions?scope_id=10.0.1.0
# Returns exclusion ranges for the given scope.
# scope_id query parameter is required.
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/exclusions' -Method GET @_epRead -Endpoint ([scriptblock]::Create($H + {
    try {
        if ($scope_id) {
            # Per-scope: flat list (backward-compatible for push path / older plugin versions).
            if (-not (Test-IPv4 $scope_id)) { Write-InvalidIPv4 'scope_id'; return }
            $null = Get-DhcpServerv4Scope -ScopeId $scope_id -ErrorAction Stop
            $exclusions = Get-DhcpServerv4ExclusionRange -ScopeId $scope_id -ErrorAction Stop
            $result = @(
                $exclusions | ForEach-Object { ConvertTo-ExclusionRangeObject $_ }
            )
            ConvertTo-Json -InputObject $result -Depth 4 -Compress
        } else {
            # Bulk: nested dict keyed by scope_id (plugin 1.4.0+ bulk pull path).
            $allExclusions = Get-DhcpServerv4ExclusionRange -ErrorAction Stop
            $result = @{}
            foreach ($ex in @($allExclusions | Where-Object { $_ -ne $null })) {
                $sid = $ex.ScopeId.ToString()
                if (-not $result.ContainsKey($sid)) { $result[$sid] = @() }
                $result[$sid] += ConvertTo-ExclusionRangeObject $ex
            }
            ConvertTo-Json -InputObject $result -Depth 4 -Compress
        }
    }
    catch [Microsoft.Management.Infrastructure.CimException] {
        Write-ApiError -Message 'Scope not found.' -StatusCode 404
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ---------------------------------------------------------------------------
# POST /api/dhcp/exclusions
# Creates a new exclusion range on a scope.
#
# Expected body:
#   {
#     "scope_id":  "10.0.1.0",
#     "start_ip":  "10.0.1.50",
#     "end_ip":    "10.0.1.59"
#   }
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/exclusions' -Method POST @_epWrite -Endpoint ([scriptblock]::Create($H + {
    try {
        $body = $Body | ConvertFrom-Json

        if (-not $body.scope_id -or -not $body.start_ip -or -not $body.end_ip) {
            New-PSUApiResponse -StatusCode 400 `
                -Body (@{ error = 'scope_id, start_ip, and end_ip are required.' } | ConvertTo-Json -Compress) `
                -ContentType 'application/json'
            return
        }
        if (-not (Test-IPv4 $body.scope_id)) { Write-InvalidIPv4 'scope_id'; return }
        if (-not (Test-IPv4 $body.start_ip)) { Write-InvalidIPv4 'start_ip'; return }
        if (-not (Test-IPv4 $body.end_ip))   { Write-InvalidIPv4 'end_ip'; return }

        Add-DhcpServerv4ExclusionRange `
            -ScopeId    $body.scope_id `
            -StartRange $body.start_ip `
            -EndRange   $body.end_ip `
            -ErrorAction Stop

        $exclusions = Get-DhcpServerv4ExclusionRange -ScopeId $body.scope_id -ErrorAction SilentlyContinue |
                      Where-Object { $_.StartRange -eq $body.start_ip -and $_.EndRange -eq $body.end_ip } |
                      Select-Object -First 1

        New-PSUApiResponse -StatusCode 201 `
            -Body (ConvertTo-ExclusionRangeObject $exclusions | ConvertTo-Json -Depth 4 -Compress) `
            -ContentType 'application/json'
    }
    catch [Microsoft.Management.Infrastructure.CimException] {
        Write-ApiError -Message 'Scope not found.' -StatusCode 404
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))


# ---------------------------------------------------------------------------
# DELETE /api/dhcp/exclusions
# Removes an exclusion range identified by scope_id + start_ip + end_ip.
# Windows DHCP has no per-exclusion ID; the tuple uniquely identifies it.
#
# Expected body:
#   {
#     "scope_id":  "10.0.1.0",
#     "start_ip":  "10.0.1.50",
#     "end_ip":    "10.0.1.59"
#   }
# ---------------------------------------------------------------------------
New-PSUEndpoint -Url '/api/dhcp/exclusions' -Method DELETE @_epWrite -Endpoint ([scriptblock]::Create($H + {
    try {
        $body = $Body | ConvertFrom-Json

        if (-not $body.scope_id -or -not $body.start_ip -or -not $body.end_ip) {
            New-PSUApiResponse -StatusCode 400 `
                -Body (@{ error = 'scope_id, start_ip, and end_ip are required.' } | ConvertTo-Json -Compress) `
                -ContentType 'application/json'
            return
        }
        if (-not (Test-IPv4 $body.scope_id)) { Write-InvalidIPv4 'scope_id'; return }
        if (-not (Test-IPv4 $body.start_ip)) { Write-InvalidIPv4 'start_ip'; return }
        if (-not (Test-IPv4 $body.end_ip))   { Write-InvalidIPv4 'end_ip'; return }

        Remove-DhcpServerv4ExclusionRange `
            -ScopeId    $body.scope_id `
            -StartRange $body.start_ip `
            -EndRange   $body.end_ip `
            -ErrorAction Stop

        New-PSUApiResponse -StatusCode 204
    }
    catch [Microsoft.Management.Infrastructure.CimException] {
        Write-ApiError -Message 'Scope not found.' -StatusCode 404
    }
    catch {
        Write-ApiError -Message $_.Exception.Message -StatusCode 500
    }
}.ToString()))
