# netbox-windows-dhcp

A NetBox v4.5.0+ plugin for full integration with Windows DHCP Server via [PowerShell Universal](https://ironmansoftware.com/powershell-universal).

## Features

- Define Windows DHCP Servers (hostname, port, HTTP/HTTPS, optional API key)
- Define DHCP Failover relationships (Load Balance or Hot Standby)
- Correlate DHCP Scopes with NetBox Prefixes (many scopes → one prefix)
- Global library of reusable DHCP Option Values with Option Code Definitions (pre-populated with all Windows DHCP built-in codes)
- Sync leases and reservations from DHCP server → NetBox IP Addresses (active mode)
- Push reservations and scope config from NetBox → DHCP server (optional)
- "DHCP Scopes" panel injected into NetBox Prefix detail views
- Full REST API for all plugin objects
- Scheduled background sync + manual "Sync Now" per server
- On-save signal to push scope changes when `push_scope_info` is enabled

## Requirements

- NetBox >= 4.5.0
- Python >= 3.10
- `requests` >= 2.28
- PowerShell Universal running on the Windows DHCP server (see PSU API Contract below)

## Installation

1. **Install the package:**
   ```bash
   pip install netbox-windows-dhcp
   ```
   Or from source:
   ```bash
   pip install -e /path/to/netbox-windows-dhcp
   ```

2. **Add to NetBox configuration (`configuration.py`):**
   ```python
   PLUGINS = [
       'netbox_windows_dhcp',
   ]

   PLUGINS_CONFIG = {
       'netbox_windows_dhcp': {
           'scope_sync_mode': 'passive',   # 'active' or 'passive'
           'push_reservations': False,
           'push_scope_info': False,
           'sync_interval': 60,            # minutes
       }
   }
   ```

3. **Add custom IP Address statuses** (required for active sync mode):
   ```python
   FIELD_CHOICES = {
       'ipam.IPAddress.status+': [
           ('dhcp-lease',    'DHCP-Lease',    'blue'),
           ('dhcp-reserved', 'DHCP-Reserved', 'cyan'),
       ]
   }
   ```

4. **Run migrations:**
   ```bash
   python manage.py migrate
   ```

5. **Restart NetBox** (gunicorn/uwsgi and RQ workers).

## IP Address Status Mapping

| Condition | `IPAddress.status` |
|---|---|
| IP falls within a scope range (no lease or reservation) | `dhcp` |
| Active DHCP lease found on server | `dhcp-lease` |
| DHCP reservation found on server | `dhcp-reserved` |

## Sync Modes

| Setting | Behavior |
|---|---|
| `passive` (default) | Pull and display scope info; do **not** update IP Address objects |
| `active` | Pull leases + reservations; update NetBox IP Address status, dns_name, description |

## PSU API Contract

The plugin expects a PowerShell Universal instance on each DHCP server exposing the following endpoints under `/api/dhcp/`:

| Method | Path | Description |
|---|---|---|
| GET | `/scopes` | List all scopes |
| GET | `/scopes/{scope_id}` | Get single scope |
| POST | `/scopes` | Create scope |
| PUT | `/scopes/{scope_id}` | Update scope |
| GET | `/leases[?scope_id=]` | List active leases |
| GET | `/reservations[?scope_id=]` | List reservations |
| POST | `/reservations` | Create reservation |
| PUT | `/reservations/{client_id}` | Update reservation |
| DELETE | `/reservations/{client_id}` | Delete reservation |
| GET | `/failover` | List failover relationships |
| POST | `/failover` | Create failover |
| GET | `/options/server` | Server-level options |
| GET | `/options/scope/{scope_id}` | Scope-level options |

Authentication: PSU v5 uses JWT App Tokens. Include `Authorization: Bearer <app_token>` if authentication is enabled. Generate tokens in the PSU admin console under **Security → App Tokens** and paste the value into the **App Token** field on the DHCPServer object in NetBox.

### Example PSU Endpoint Script

```powershell
# GET /api/dhcp/scopes
New-PSUEndpoint -Url "/api/dhcp/scopes" -Method "GET" -Endpoint {
    Get-DhcpServerv4Scope | ForEach-Object {
        @{
            scope_id    = $_.ScopeId.ToString()
            name        = $_.Name
            start_ip    = $_.StartRange.ToString()
            end_ip      = $_.EndRange.ToString()
            subnet_mask = $_.SubnetMask.ToString()
            description = $_.Description
        }
    } | ConvertTo-Json
}
```

## Navigation

The plugin adds a **Windows DHCP** menu to the NetBox left sidebar with:
- **Infrastructure** → Servers, Failover
- **Scopes** → Scopes
- **Options** → Option Values, Option Code Definitions
- **Admin** → Settings

## REST API

Available at `/api/plugins/netbox-windows-dhcp/`:
- `GET/POST /servers/`
- `GET/POST /failover/`
- `GET/POST /option-codes/`
- `GET/POST /option-values/`
- `GET/POST /scopes/`
