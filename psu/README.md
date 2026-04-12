# PSU DHCP API Endpoints

PowerShell Universal endpoint definitions for the `netbox-windows-dhcp` plugin.

## Prerequisites

- Windows Server with the **DHCP Server** role installed
- **PowerShell Universal v5.x** (tested on 5.6.11+) running on the DHCP server
- The `DhcpServer` PowerShell module (ships with the DHCP Server role)

## Deployment

### Step 1 — Add the script to PSU

The recommended approach is to place `dhcp_api_endpoints.ps1` in your PSU Git repository so it is version-controlled and automatically picked up on sync.

Alternatively, in the PowerShell Universal admin UI:

1. Go to **APIs → Scripts**
2. Create a new script and paste in the contents of `dhcp_api_endpoints.ps1`

PSU will register all endpoints defined in the script on the next restart or reload.

### Step 2 — Configure Authentication (recommended)

PSU v5 uses JWT App Tokens for authentication.

1. In the PSU admin console, go to **Security → App Tokens** and generate a new token
2. Copy the token value
3. In NetBox, edit the **DHCP Server** object and paste the token into the **App Token** field
4. Add `-Authentication` to each `New-PSUEndpoint` call in the script to enforce the token

Without `-Authentication`, the endpoints are unauthenticated and accessible to anyone who can reach the PSU server.

The plugin sends the token as:

```http
Authorization: Bearer <token>
```

### Step 3 — Verify

Test from a machine that can reach the PSU server:

```powershell
$headers = @{ Authorization = 'Bearer your-app-token-here' }
$base    = 'https://dhcp01.example.com:443/api/dhcp'

# List all scopes
Invoke-RestMethod -Uri "$base/scopes" -Headers $headers | ConvertTo-Json -Depth 4

# List leases for a specific scope
Invoke-RestMethod -Uri "$base/leases?scope_id=10.0.1.0" -Headers $headers | ConvertTo-Json -Depth 4
```

Or trigger **Sync Now** from the plugin's Server detail page in NetBox and view the job log.

## Script Architecture

PSU v5 runs each endpoint in an isolated runspace, so functions defined in one endpoint are not available in another. The script handles this by storing shared helper functions in the `$H` string and prepending them to every endpoint's scriptblock via `[scriptblock]::Create($H + {...}.ToString())`. This makes each endpoint fully self-contained.

Shared helpers defined in `$H`:

- `ConvertTo-ScopeObject` — maps a DHCP scope CIM object to the API response shape
- `ConvertTo-LeaseObject` — maps a lease CIM object; only returns leases with `address_state` of `Active` or `ActiveReservation`
- `ConvertTo-ReservationObject` — maps a reservation CIM object
- `ConvertTo-FailoverObject` — maps a failover CIM object; resolves the local server's FQDN for `primary_server`
- `ConvertTo-OptionValueObject` — maps a DHCP option value CIM object
- `Find-ReservationByClientId` — searches all scopes for a reservation by client MAC address
- `Write-ApiError` — returns a JSON error response with a given HTTP status code

All list endpoints use `ConvertTo-Json -InputObject $result` (not `$result | ConvertTo-Json`) to prevent PowerShell's pipeline from unwrapping single-element arrays into bare objects.

URL path parameters (`:param`) and the `$Body` variable are injected automatically by PSU — no `param()` declaration is required inside endpoint scriptblocks.

## Endpoint Reference

| Method | URL | Description |
| --- | --- | --- |
| GET | `/api/dhcp/scopes` | List all scopes (includes `router` and `failover_name`) |
| GET | `/api/dhcp/scopes/:scope_id` | Get single scope by network address |
| POST | `/api/dhcp/scopes` | Create a scope |
| PUT | `/api/dhcp/scopes/:scope_id` | Update a scope |
| GET | `/api/dhcp/leases` | List active leases (optional `?scope_id=`) |
| GET | `/api/dhcp/reservations` | List reservations (optional `?scope_id=`) |
| POST | `/api/dhcp/reservations` | Create a reservation |
| PUT | `/api/dhcp/reservations/:client_id` | Update reservation by MAC |
| DELETE | `/api/dhcp/reservations/:client_id` | Delete reservation by MAC — returns 204 |
| GET | `/api/dhcp/failover` | List failover relationships |
| POST | `/api/dhcp/failover` | Create a failover relationship |
| GET | `/api/dhcp/options/server` | Server-level option values |
| GET | `/api/dhcp/options/scope/:scope_id` | Scope-level option values |

## Response Shapes

### Scope

```json
{
  "scope_id": "10.0.1.0",
  "name": "Building A",
  "start_ip": "10.0.1.10",
  "end_ip": "10.0.1.254",
  "subnet_mask": "255.255.255.0",
  "description": "",
  "state": "Active",
  "lease_duration_seconds": 86400,
  "router": "10.0.1.1",
  "failover_name": "FAILOVER-BUILDING-A"
}
```

`router` is read from DHCP Option 3 on the scope; `null` if not set. `failover_name` is `null` if the scope is not part of a failover relationship.

### Lease

```json
{
  "ip_address": "10.0.1.50",
  "client_id": "00-11-22-33-44-55",
  "hostname": "DESKTOP-ABC123",
  "scope_id": "10.0.1.0",
  "lease_expiry": "2026-04-12T00:00:00Z",
  "address_state": "Active"
}
```

Only leases with `address_state` of `Active` or `ActiveReservation` are returned.

### Reservation

```json
{
  "ip_address": "10.0.1.100",
  "client_id": "00-11-22-33-44-55",
  "name": "printer-01",
  "description": "",
  "type": "Dhcp",
  "scope_id": "10.0.1.0"
}
```

### Failover

```json
{
  "name": "FAILOVER-BUILDING-A",
  "primary_server": "dhcp01.example.com",
  "secondary_server": "dhcp02.example.com",
  "mode": "LoadBalance",
  "scope_ids": ["10.0.1.0", "10.0.2.0"],
  "max_client_lead_time": 3600,
  "max_response_delay": 30,
  "state_switchover_interval": null,
  "enable_auth": false
}
```

`primary_server` is resolved from the local machine's FQDN. `state_switchover_interval` is `null` when automatic state switchover is disabled.

### Option Value

```json
{
  "code": 6,
  "name": "DNS Servers",
  "value": ["8.8.8.8", "8.8.4.4"],
  "type": "IPv4Address",
  "vendor_class": ""
}
```

`value` is always an array. Multi-value options (e.g. DNS server lists) contain multiple entries.

## Notes

- **`scope_id`** is always the network address of the scope (e.g. `10.0.1.0`), matching the Windows DHCP Server convention.
- **`client_id`** uses Windows DHCP hyphen-separated hex format: `00-11-22-33-44-55`. The `POST /reservations` endpoint normalises any MAC format to this convention automatically.
- **Failover creation** (`POST /api/dhcp/failover`) must be run against the **primary** server. The `Add-DhcpServerv4Failover` cmdlet contacts the secondary server directly from the primary.
- **Option 3 (Router)** is not returned in scope-level option value responses — it is included directly in the scope object as `router`. The NetBox plugin stores it on the Scope's `router` field and skips it during option value import.
- **Option 51 (Lease Time)** is also skipped during option value import — it is stored on the Scope's `lease_lifetime` field (in seconds) and displayed in the NetBox UI as the most readable exact unit (e.g. `3 Days`, `73 Hours`).
