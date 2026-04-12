# PSU DHCP API Endpoints

PowerShell Universal endpoint definitions for the `netbox-windows-dhcp` plugin.

## Prerequisites

- Windows Server with the **DHCP Server** role installed
- **PowerShell Universal** (v3.x or v4.x) running on the DHCP server
- The `DhcpServer` PowerShell module (ships with the DHCP Server role)

## Deployment

### Step 1 — Add the script to PSU

In the PowerShell Universal admin UI:

1. Go to **APIs → Scripts**
2. Create a new script (or place `dhcp_api_endpoints.ps1` directly in your PSU repository folder under `endpoints/`)
3. Paste or import the contents of `dhcp_api_endpoints.ps1`

Alternatively, if using a PSU Git repository, commit `dhcp_api_endpoints.ps1` to the root of the repo and PSU will pick it up on the next sync.

### Step 2 — Configure Authentication (recommended)

1. In PSU, go to **Security → API Keys** and create a new key
2. Copy the key value
3. In NetBox, edit the **DHCP Server** object and paste the key in the **API Key** field
4. Add `-Authentication` to each `New-PSUEndpoint` call in the script to enforce the key

Without `-Authentication`, the endpoints are unauthenticated and accessible to anyone who can reach the server.

### Step 3 — Verify

Test from a machine that can reach the PSU server:

```powershell
# PSU v5: App Token passed as a Bearer token
$headers = @{ Authorization = 'Bearer your-app-token-here' }
$base    = 'https://dhcp01.example.com:443/api/dhcp'

# List all scopes
Invoke-RestMethod -Uri "$base/scopes" -Headers $headers | ConvertTo-Json

# List leases for a scope
Invoke-RestMethod -Uri "$base/leases?scope_id=10.0.1.0" -Headers $headers | ConvertTo-Json
```

Or trigger a **Sync Now** from the plugin's Server detail page inside NetBox.

## Endpoint Reference

| Method | URL | Description |
|--------|-----|-------------|
| GET | `/api/dhcp/scopes` | List all scopes |
| GET | `/api/dhcp/scopes/:scope_id` | Get single scope by network address |
| POST | `/api/dhcp/scopes` | Create a scope |
| PUT | `/api/dhcp/scopes/:scope_id` | Update a scope |
| GET | `/api/dhcp/leases` | List active leases (optional `?scope_id=`) |
| GET | `/api/dhcp/reservations` | List reservations (optional `?scope_id=`) |
| POST | `/api/dhcp/reservations` | Create a reservation |
| PUT | `/api/dhcp/reservations/:client_id` | Update reservation by MAC |
| DELETE | `/api/dhcp/reservations/:client_id` | Delete reservation by MAC |
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
  "lease_duration_seconds": 86400
}
```

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
  "partner_server": "dhcp02.example.com",
  "mode": "LoadBalance",
  "scope_ids": ["10.0.1.0", "10.0.2.0"],
  "max_client_lead_time": 3600,
  "max_response_delay": 30,
  "state_switchover_interval": null,
  "enable_auth": false
}
```

### Option Value
```json
{
  "option_id": 6,
  "name": "DNS Servers",
  "value": ["8.8.8.8", "8.8.4.4"],
  "type": "IPv4Address",
  "vendor_class": ""
}
```

## Notes

- **`scope_id`** is always the network address of the scope (e.g. `10.0.1.0`), matching Windows DHCP's own convention.
- **`client_id`** uses Windows DHCP format: `00-11-22-33-44-55` (hyphen-separated hex octets). The POST /reservations endpoint normalises any MAC format to this convention automatically.
- **Failover creation** (`POST /api/dhcp/failover`) must run on the **primary** server. PSU on the primary server contacts the secondary server directly via the `Add-DhcpServerv4Failover` cmdlet.
- The DELETE reservation endpoint returns **HTTP 204** (no body) on success, which the NetBox plugin's `PSUClient` handles correctly.
