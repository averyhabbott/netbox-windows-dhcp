# netbox-windows-dhcp

A NetBox v4.5.0+ plugin for full integration with Windows DHCP Server via [PowerShell Universal](https://ironmansoftware.com/powershell-universal) (PSU v5).

## Features

- Define Windows DHCP Servers (hostname, port, HTTP/HTTPS, App Token auth, optional SSL verification)
- Define DHCP Failover relationships (Load Balance or Hot Standby)
- Correlate DHCP Scopes with NetBox Prefixes (many scopes → one prefix)
- Global library of reusable DHCP Option Values with Option Code Definitions (pre-populated with all standard Windows DHCP built-in codes)
- One-time **Import from Server** — pulls failovers, scopes, and scope-level option values from a live DHCP server into NetBox
- Sync leases and reservations from DHCP server → NetBox IP Addresses (active sync mode)
- Updates IP Address `status`, `dns_name`, and the `dhcp_client_id` custom field (auto-registered on first run)
- Push reservations and scope config from NetBox → DHCP server (optional)
- **DHCP Scopes** panel injected into the NetBox Prefix detail view (left column, below Prefix details)
- Full REST API for all plugin objects
- Scheduled background sync (hourly) + manual **Sync Now** per server or for all servers
- Plugin-wide settings managed via the NetBox UI (no `configuration.py` entries required beyond installation)

## Requirements

- NetBox >= 4.5.0
- Python >= 3.10
- `requests` >= 2.28
- PowerShell Universal **v5.x** running on the Windows DHCP server (see [PSU Setup](#psu-setup))

## Installation

### 1. Install the package

```bash
pip install netbox-windows-dhcp
```

Or from source:

```bash
pip install -e /path/to/netbox-windows-dhcp
```

### 2. Add to NetBox configuration (`configuration.py`)

```python
PLUGINS = [
    'netbox_windows_dhcp',
]
```

No `PLUGINS_CONFIG` entry is required. All settings are managed through the plugin's **Admin → Settings** page in the NetBox UI.

### 3. Add custom IP Address statuses

Active sync mode sets IP Address status to `dhcp-lease` or `dhcp-reserved`. These values must be added to NetBox's field choices:

```python
FIELD_CHOICES = {
    'ipam.IPAddress.status+': [
        ('dhcp-lease',    'DHCP-Lease',    'blue'),
        ('dhcp-reserved', 'DHCP-Reserved', 'cyan'),
    ]
}
```

If these are missing, the plugin will display a warning on the Settings page and log a warning at startup.

### 4. Run migrations

```bash
python manage.py migrate
```

Running migrations also triggers the `post_migrate` signal which auto-registers the `dhcp_client_id` custom field on IP Address objects.

### 5. Restart NetBox

Restart gunicorn/uwsgi and the RQ workers.

## Getting Started

### Add a DHCP Server

1. Go to **Windows DHCP → Infrastructure → Servers → Add**
2. Enter the hostname/IP, port, and PSU App Token
3. Disable **Verify SSL Certificate** if using a self-signed cert

### Import from a Server

Once a server is configured, use **Import from Server** (available on the server detail page) to do a one-time pull of:

- Failover relationships (matched to existing DHCP Server objects by hostname)
- Scopes (NetBox Prefixes are created automatically if they don't exist)
- Scope-level option values (unknown option codes are created automatically)
  - Option 3 (Router) and Option 51 (Lease Time) are skipped — they are stored directly on the Scope object

Existing records are skipped; the import never overwrites data already in NetBox.

### Configure Sync Settings

Go to **Windows DHCP → Admin → Settings** to configure:

| Setting | Description |
| --- | --- |
| Scope Data Sync Mode | `passive` (default) — scope info only; `active` — update IP Addresses |
| Push Reservations to DHCP Server | Push NetBox `reserved`/`dhcp-reserved` IPs to the DHCP server |
| Push Scope Info to DHCP Server | Push scope config changes from NetBox to the DHCP server |
| Sync Interval (minutes) | How often the background sync runs (5–1440) |

## Sync Behavior

### Sync modes

| Mode | Behavior |
| --- | --- |
| `passive` (default) | Pull and display scope info; do **not** update IP Address objects |
| `active` | Pull leases and reservations; create or update NetBox IP Address status, `dns_name`, and `dhcp_client_id` |

### IP Address status mapping (active mode)

| Source | `IPAddress.status` |
| --- | --- |
| Active DHCP lease | `dhcp-lease` |
| DHCP reservation | `dhcp-reserved` |

Reservations take precedence — if an IP has a reservation, it will not be overwritten by a lease record.

New IP Addresses are created using the prefix length of the associated scope's NetBox Prefix (not `/32`).

The `dhcp_client_id` custom field (auto-registered on IP Address) stores the client MAC address in Windows DHCP format (`00-11-22-33-44-55`).

### Sync Now

The **Sync Now** button on a server's detail page (and the sync icon in the server list) runs the sync synchronously and displays the result as a success or error message immediately.

## Navigation

The plugin adds a **Windows DHCP** menu to the NetBox left sidebar:

- **Infrastructure** → Servers, Failover
- **Scopes** → Scopes
- **Options** → Option Values, Option Code Definitions
- **Admin** → Settings

## PSU Setup

See [psu/README.md](psu/README.md) for full deployment instructions.

The plugin expects PowerShell Universal **v5.x** on each DHCP server, exposing endpoints under `/api/dhcp/`. The PSU script is at `psu/dhcp_api_endpoints.ps1`.

### Endpoint reference

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/dhcp/scopes` | List all scopes (includes router and failover name) |
| GET | `/api/dhcp/scopes/:scope_id` | Get a single scope |
| POST | `/api/dhcp/scopes` | Create a scope |
| PUT | `/api/dhcp/scopes/:scope_id` | Update a scope |
| GET | `/api/dhcp/leases[?scope_id=]` | List active leases |
| GET | `/api/dhcp/reservations[?scope_id=]` | List reservations |
| POST | `/api/dhcp/reservations` | Create a reservation |
| PUT | `/api/dhcp/reservations/:client_id` | Update a reservation |
| DELETE | `/api/dhcp/reservations/:client_id` | Delete a reservation |
| GET | `/api/dhcp/failover` | List failover relationships |
| POST | `/api/dhcp/failover` | Create a failover relationship |
| GET | `/api/dhcp/options/server` | Server-level option values |
| GET | `/api/dhcp/options/scope/:scope_id` | Scope-level option values |

Authentication: PSU v5 App Tokens are sent as `Authorization: Bearer <token>`. Generate a token in the PSU admin console under **Security → App Tokens** and paste it into the **App Token** field on the DHCP Server object in NetBox.

## REST API

The plugin exposes a REST API under `/api/plugins/netbox-windows-dhcp/`:

| Endpoint | Models |
| --- | --- |
| `/servers/` | DHCP Servers |
| `/failover/` | Failover relationships |
| `/option-codes/` | Option Code Definitions |
| `/option-values/` | Option Values |
| `/scopes/` | DHCP Scopes |
