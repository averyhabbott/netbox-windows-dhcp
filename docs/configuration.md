# Configuration

Most settings live in NetBox's UI:

- **Settings** (**Windows DHCP → Admin → Settings**): plugin-wide behavior.
- **Schedule** (**Windows DHCP → Admin → Schedule**): when the recurring sync runs.
- **Each DHCP server and failover:** a few settings of their own.

Every save of the Settings page is recorded in NetBox's changelog, with who changed what.

`configuration.py` can override some of them (see [PLUGINS_CONFIG overrides](#plugins_config-overrides)).

> Throughout the docs, `dhcp` and `reserved` stand for the **DHCP Lease Status** and **DHCP Reservation Status** you pick here. Every rule applies to the statuses you configure.

## Settings page

### Leases & Reservations

**Pulled from the DHCP server**

| Setting | Default | What it does |
| --- | --- | --- |
| Sync IP Addresses from Leases & Reservations | Off | Turns leases and reservations into NetBox IP Addresses, keeps them up to date, and removes the ones the server no longer has (see [How the sync works](sync-logic.md#ip-addresses)). Off, the sync handles scopes only. The IP editing lock only applies while this is on. |
| DHCP Lease Status | `dhcp` | The IP Address status for leases. Changing it later makes the next sync update every lease IP to the new status. Any NetBox status works, including custom ones from `FIELD_CHOICES`. |
| DHCP Reservation Status | `reserved` | The IP Address status for reservations. With Push Reservations on, IPs with this status are what get pushed. |

**Pushed to the DHCP server**

| Setting | Default | What it does |
| --- | --- | --- |
| Push Reservations to DHCP Server | Off | On: NetBox is the source of truth for reservations. Reserved IPs are created, updated and deleted on the server, immediately on save and on every sync, and server reservations NetBox doesn't have are deleted. Off: the server is the source of truth, and NetBox mirrors its reservations. |
| Placeholder Reservations | Off | With Push Reservations on, a reserved IP inside a scope's range with no client ID gets a made-up one (`ba-dc-0d-ed-` + 8 hex digits), saved in NetBox and pushed, so the server stops handing that IP out. A real MAC entered later replaces it. Turning this off stops new placeholders; existing ones stay. |

**Protected IP Addresses**

| Setting | Default | What it does |
| --- | --- | --- |
| Sync-Protected Tag | None | IPs carrying this tag, and every IP inside a prefix carrying it, are never changed or deleted by the sync. It doesn't stop pushes to the server. See [Operations](operations.md#protected-ips). |
| Update Client ID for Protected IPs | Off | Lets the sync update just the client ID of a protected IP from the server's active lease (useful after replacing a server, when client IDs change). Everything else stays protected. |

### Scopes

| Setting | Default | What it does |
| --- | --- | --- |
| Push Scope Info to DHCP Server | Off | On: NetBox is the source of truth for scopes (name, description, range, router, lease time, active state, failover, options and exclusions). Changes are pushed on save and on every sync, NetBox-only scopes are created on the server, and server-only scopes are deleted from it. Off: the server is the source of truth. The sync pulls every scope's settings, imports new server scopes, and deletes NetBox scopes the server no longer has. Maintenance mode and the per-server and per-failover sync settings apply either way. |
| Create Missing Prefixes on Import | On | When a learned scope has no prefix in NetBox, create one in the Default Scope VRF. Off: the scope is created without a prefix instead and appears under Unassigned Scopes. Turn it off if prefixes come from another source. |

### Global

| Setting | Default | What it does |
| --- | --- | --- |
| Sync Interval (minutes) | 60 | How often the recurring sync runs (5–1440). A change applies the next time you click **Schedule**, or when the recurring sync next reschedules itself. |
| Sync Job Queue | Default | The RQ queue for the plugin's sync, import, push and PSU script update jobs (High, Default or Low) |
| Sync Job Timeout (seconds) | 300 | How long a sync job may run before RQ stops it. Raise it for servers with many scopes. Push, import and PSU script update jobs use NetBox's `RQ_DEFAULT_TIMEOUT` instead. |
| Sync Job Log Level | Debug | The lowest level written to the log of the sync, server sync, scope push, scope delete and reservation push jobs. A higher level makes large logs smaller. The Import and Update PSU Scripts jobs always log everything. |
| API Enabled | On | Off: every plugin REST API endpoint returns 503 Service Unavailable. |

Saving settings never starts, stops or reschedules a sync.

## Server settings

On each DHCP server's edit page, besides the connection details:

| Setting | What it does |
| --- | --- |
| Sync Standalone Scopes | Whether this server's scopes that aren't in a failover are synced. Failover scopes follow their failover's **Sync Enabled** instead. |
| Default Scope VRF | The VRF that scopes learned from this server (standalone scopes) go into, and where Import and the sync look for their prefix. Blank is Global. Changing it only affects scopes learned from then on. |

### Read-only API keys

A server whose App Token can only read (the `DHCPReader` role; shown as read-only in the **Writable** column) **overrides both push settings for that server:** it always syncs as "server wins". That includes deleting NetBox scopes the server doesn't have, and NetBox-only reserved IPs in its ranges. Push jobs skip it with one log line.

This is on purpose. For example, a test NetBox can mirror production DHCP servers with read-only tokens without ever changing them. Don't give a read-only token to a server you expect NetBox to push to.

## Failover settings

Failovers come from **Import from Server**. Their Windows settings (servers, mode, timers) are read-only in NetBox. On the failover you can change:

| Setting | What it does |
| --- | --- |
| Sync Enabled | Whether the failover's scopes are synced. Toggle it from the Failover list or the failover's page. |
| Default Scope VRF | The VRF learned scopes in this failover go into. Blank is Global. |
| Description, tags, custom fields | NetBox only |

## Schedule page

| Card | Button | What it does |
| --- | --- | --- |
| Scheduled Sync | **Schedule** | Runs the first sync at the date and time you pick, then repeats on the Sync Interval. It replaces any existing schedule. |
| On-Demand | **Run Now** | Runs one sync of every server right away. It doesn't repeat and doesn't change the schedule. |

The page shows the next scheduled run and its interval, or "no sync scheduled".

- Restarting NetBox or its workers never creates or changes a schedule. A new install has none until you click **Schedule**.
- The recurring **Windows DHCP Sync** job always runs as `DHCP-Sync-Service`. Clicking either button creates a short **Set DHCP Sync Schedule** job under your own name.
- To stop the recurring sync, delete the scheduled **Windows DHCP Sync** job in NetBox's Jobs list.
- To sync a single server, use **Sync Now** on its page.

## PLUGINS_CONFIG overrides

`configuration.py` can override some settings without changing the database. The main use: a test NetBox running on a copy of the production database, which must use its own credentials and must never push to production.

### Per-server API key

```python
PLUGINS_CONFIG = {
    'netbox_windows_dhcp': {
        'server_overrides': {
            'dhcp01.example.com': {'api_key': 'test-token-here'},
            'dhcp02.example.com': {'api_key': 'test-token-here'},
        },
    },
}
```

The key must match the server's **Hostname** exactly. The override token is used instead of the stored one, and the server's page shows a notice.

### Certificate fetch allowlist

```python
PLUGINS_CONFIG = {
    'netbox_windows_dhcp': {
        'restrict_allowlist': True,
        'server_overrides': {
            'dhcp01.example.com': {'allowed': True},
        },
    },
}
```

With `restrict_allowlist` on, **Fetch Certificate** only works for hostnames listed with `'allowed': True`.

### Forcing sync settings

```python
PLUGINS_CONFIG = {
    'netbox_windows_dhcp': {
        'sync_ips_from_dhcp': False,   # Sync IP Addresses from Leases & Reservations
        'push_reservations': False,    # Push Reservations to DHCP Server
        'push_scope_info': False,      # Push Scope Info to DHCP Server
    },
}
```

`True` or `False` forces the setting whatever the database says; `None` (or leaving the key out) uses the saved value. A forced setting is locked on the Settings page, with a note saying so. Forcing `push_scope_info` off also applies its [editing rules](editing-rules.md).
