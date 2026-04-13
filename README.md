# netbox-windows-dhcp

A NetBox v4.5.0+ plugin for full integration with Windows DHCP Server via [PowerShell Universal](https://ironmansoftware.com/powershell-universal) (PSU v5).

## Features

- Define Windows DHCP Servers (hostname, port, HTTP/HTTPS, App Token auth, optional SSL verification)
- Define DHCP Failover relationships (Load Balance or Hot Standby)
- Correlate DHCP Scopes with NetBox Prefixes (many scopes → one prefix)
- Lease lifetime stored in seconds, displayed in the most readable unit (e.g. `3 Days`, `73 Hours`, `30 Minutes`)
- Global library of reusable DHCP Option Values with Option Code Definitions (pre-populated with all standard Windows DHCP built-in codes)
- One-time **Import from Server** — pulls failovers, scopes, and scope-level option values from a live DHCP server into NetBox
- Active sync mode: pull leases and reservations → create/update NetBox IP Addresses with status, DNS name, and client MAC
- Stale record cleanup: expired leases and removed reservations are deleted or downgraded automatically
- Push reservations and scope config from NetBox → DHCP server (optional, settings-controlled)
- **DHCP Scopes** panel injected into the NetBox Prefix detail view
- All sync and import operations run as background jobs — no HTTP timeouts on large servers
- All background job changes appear in the NetBox changelog (attributed to the user who queued the job)
- Scheduled background sync (hourly by default, configurable) + manual **Sync Now** per server or for all servers
- Full REST API for all plugin objects
- Plugin-wide settings managed via the NetBox UI (no `PLUGINS_CONFIG` entry required)

## Requirements

- NetBox >= 4.5.0
- Python >= 3.10
- `requests` >= 2.28
- PowerShell Universal **v5.x** running on each Windows DHCP server (see [PSU Setup](#psu-setup))

## Installation

### 1. Install the package

```bash
pip install netbox-windows-dhcp
```

Or from source (editable install recommended for development):

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

Active sync mode sets IP Address status to `dhcp-lease` or `dhcp-reserved`. These values must be added to NetBox's field choices in `configuration.py`:

```python
FIELD_CHOICES = {
    'ipam.IPAddress.status+': [
        ('dhcp-lease',    'DHCP-Lease',    'blue'),
        ('dhcp-reserved', 'DHCP-Reserved', 'cyan'),
        ('dhcp-staged',   'DHCP-Staged',   'green'),  # optional — see Pre-Staging IPs below
    ]
}
```

`dhcp-lease` and `dhcp-reserved` are required when **Sync IP Addresses from Leases & Reservations** is enabled. If these are missing, the plugin displays a warning on the Settings page and logs a warning at startup.

`dhcp-staged` is optional and only needed if you use the pre-staging workflow described below.

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
3. Toggle **Use HTTPS** as appropriate (default: on)
4. Disable **Verify SSL Certificate** if the server uses a self-signed certificate

### Import from a Server

Once a server is configured, click **Import from Server** on the server detail page to run a one-time background import of:

- Failover relationships (matched to existing DHCP Server objects by hostname)
- Scopes (NetBox Prefixes are created automatically if they don't exist)
- Scope-level option values (unknown option codes are created automatically)
  - Option 3 (Router) and Option 51 (Lease Time) are skipped — they are stored directly on the Scope object

You will be redirected to the job status page where you can monitor progress and view the full results log. Existing records are skipped; the import never overwrites data already in NetBox.

### Configure Sync Settings

Go to **Windows DHCP → Admin → Settings** to configure:

| Setting | Description |
| --- | --- |
| Sync IP Addresses from Leases & Reservations | When checked, pull leases and reservations and create/update/delete NetBox IP Address records. When unchecked, sync scope config only. |
| Push Reservations to DHCP Server | Push NetBox `reserved`/`dhcp-reserved` IPs to the DHCP server as reservations |
| Push Scope Info to DHCP Server | Push scope config changes from NetBox to the DHCP server |
| Sync Interval (minutes) | How often the background sync runs (5–1440) |

## Sync Behavior

### IP Address sync

When **Sync IP Addresses from Leases & Reservations** is unchecked (default), the sync pulls and stores scope config only — no IP Address objects are created or modified.

When checked, the sync pulls leases and reservations from each DHCP server and creates/updates/deletes NetBox IP Address records.

### IP Address lifecycle (active sync)

**Creating and updating:**

| Source | `IPAddress.status` | `dns_name` | `dhcp_client_id` |
| --- | --- | --- | --- |
| Active DHCP lease | `dhcp-lease` | Hostname from DHCP (lowercased) | Client MAC address |
| DHCP reservation | `dhcp-reserved` | Name from reservation (lowercased) | Client MAC address |

Reservations take precedence — a `dhcp-reserved` IP is not overwritten by a lease record for the same address.

New IP Addresses are created using the prefix length of the associated scope's NetBox Prefix (not `/32`).

The `dhcp_client_id` custom field (auto-registered on IP Address) stores the client MAC address in Windows DHCP hyphen-separated format (`00-11-22-33-44-55`).

All IP Address creates and updates are recorded in the NetBox changelog, attributed to the user who queued the sync job.

**Cleanup (stale record removal):**

After syncing leases and reservations for a scope, the plugin removes records that no longer exist on the server:

| NetBox status | Server state | Action |
| --- | --- | --- |
| `dhcp-reserved` | Reservation still exists | No change |
| `dhcp-reserved` | No reservation, but lease exists | Downgraded to `dhcp-lease` |
| `dhcp-reserved` | Neither reservation nor lease | Deleted |
| `dhcp-lease` | Lease still active | No change |
| `dhcp-lease` | Lease expired or gone | Deleted |

> **Note:** When **Push Reservations to DHCP Server** is enabled, NetBox is the source of truth for reservations. `dhcp-reserved` IP Addresses are never deleted or downgraded based on server state — they will be pushed to the server on the next sync instead.

**Scope cleanup:**

When a scope exists in NetBox but is no longer reported by the DHCP server, and the server was successfully reached, and **Push Scope Info to DHCP Server** is disabled, the scope is deleted from NetBox. Only scopes linked to the server via a failover relationship are considered.

> When **Push Scope Info to DHCP Server** is enabled, a scope missing from the server will instead be created on the server.

**Safety:** If the API call to fetch leases/reservations fails for a scope, cleanup is skipped for that scope. If `list_scopes()` fails entirely, the server is unreachable and no cleanup runs at all.

### Pre-Staging IPs (`dhcp-staged`)

Sometimes a device needs to be registered in NetBox weeks before it is provisioned — before a lease has been issued and before the client MAC is known (making a DHCP reservation impossible).

Set the IP Address status to `DHCP-Staged` (requires the optional `dhcp-staged` entry in `FIELD_CHOICES`) and set `dns_name` to the planned canonical hostname. The sync will never overwrite or delete a `dhcp-staged` IP:

- If a lease or reservation appears for the same address, the sync logs a skip message and leaves the IP untouched.
- Cleanup never removes `dhcp-staged` IPs regardless of server state.

**Typical lifecycle:**

1. Create the IP in NetBox with status `DHCP-Staged` and the planned `dns_name`.
2. Device is provisioned; it receives a DHCP lease. The sync skips the address and logs `"IP x.x.x.x is dhcp-staged — skipping"`.
3. Once the client MAC is known, edit the IP: set status to `DHCP-Reserved`, fill in `dhcp_client_id`, and optionally push the reservation to the DHCP server.

The planned `dns_name` is preserved throughout this process — it will not be overwritten with the DHCP-assigned hostname.

### Sync Now

The **Sync Now** button on a server detail page enqueues a background sync job and redirects to the job status page. The full sync log is visible there. A **Sync All Servers** action is also available from the server list page.

## Lease Lifetime Display

Lease lifetimes are stored in seconds and displayed in the most readable exact unit:

- `86400` → **1 Day**
- `259200` → **3 Days**
- `262800` → **73 Hours** (not an exact number of days)
- `3600` → **1 Hour**
- `60` → **1 Minute**
- `45` → **45 Seconds**

When configuring a scope, enter the value and select the unit (Seconds / Minutes / Hours / Days). The form defaults to **1 Day** for new scopes. Values learned from the DHCP server are automatically decomposed to the largest clean unit.

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

| Endpoint | Model |
| --- | --- |
| `/servers/` | DHCP Servers |
| `/failover/` | Failover relationships |
| `/option-codes/` | Option Code Definitions |
| `/option-values/` | Option Values |
| `/scopes/` | DHCP Scopes |
