# PowerShell Universal

NetBox doesn't talk to Windows DHCP directly. Each DHCP server runs PowerShell Universal (PSU), which hosts a small web API (the plugin's **endpoints**) under `/api/dhcp/`. The plugin calls that API over HTTPS, and the endpoints run the Windows `DhcpServer` cmdlets on the server.

Setting PSU up for the first time is covered in [Installation](installation.md#2-prepare-psu-on-each-dhcp-server). This page explains how the pieces fit together, and lists the endpoints for anyone calling them directly.

## How NetBox signs in

- Every request carries a PSU **App Token** (`Authorization: Bearer <token>`), stored on the DHCP Server in NetBox. A per-server override can replace it from `configuration.py` (see [Configuration](configuration.md#plugins_config-overrides)).
- Every endpoint requires a token with a role. Read endpoints accept `DHCPReader` or `DHCPWriter`; everything that changes the server needs `DHCPWriter`. Authentication can't be turned off.
- `DHCPWriter` also has PSU's `apis/*` permission, which lets **Update PSU Scripts** replace the endpoints.
- The health check tells the two apart: NetBox tries a harmless write (`POST /api/dhcp/health`), and a 403 marks the server **read-only** (the Writable column on the Servers list). A read-only server always syncs as "server wins"; see [How the sync works](sync-logic.md#read-only-api-keys).

## How the endpoints get onto the server

The endpoint script (`dhcp_api_endpoints.ps1`) ships inside the plugin package. You never copy it to the server by hand:

1. **Update PSU Scripts** (on a server's page, or as a bulk action on the Servers list) reads the bundled script.
2. It updates each endpoint already in PSU, creates new ones, and deletes endpoints the script no longer has.
3. It restarts PSU's endpoints, then runs a health check to confirm the new version is live.

The job ends as **Failed** if any step fails, so a permission or connection problem is never hidden.

Run it on every server after each plugin upgrade (see [Upgrading](upgrading.md)).

## Logging

The script writes to PSU's own log, in **Platform > Logging**, as `[Level] [DHCP-/api/dhcp/<path>:<METHOD>] step=... scope=... ip=...`.

| Level | What it records |
| --- | --- |
| Error | A request that failed with a 5xx, a batch item that failed, a `/metrics` section that couldn't be read, and a reservation that couldn't be restored. Windows error codes are included when there are any. |
| Warning | A request refused with a 4xx (bad input, scope not found), a batch item that wasn't found, and a best-effort step that failed (a scope's router, lease time, an option value, or making a lease an active reservation). The response is the same as before. |
| Information | Each change made to the DHCP server: scope, reservation (including making a lease an active reservation), exclusion and failover creates, updates and deletes. PSU already logs each request's status itself. |
| Debug | How long a bulk read took, and how many scopes and items it covered. |

PSU's **Database** logging target (Platform > Logging targets) defaults to Information, which hides the Debug lines. Set it to Debug to see them. Tokens and request bodies are never logged.

## Script version

The script carries a version number (`$PSU_SCRIPT_VERSION`, currently **2.0.1**), returned by `GET /api/dhcp/health`. The plugin compares it with the version it expects:

| Servers list shows | Meaning |
| --- | --- |
| Green check | The server runs the script this plugin version expects |
| Amber warning | A different version. Run **Update PSU Scripts**. |
| Gray question mark | No health check yet |

A server on an older script still syncs, but without the features that need the new endpoints. With a pre-2.0.0 script, the sync skips IPs, exclusions and options for that server, reservation push is create-only, and scope active state isn't pushed. The sync log says so.

The script version and the plugin version are separate numbers; the script only changes version when the script itself changes.

The script is backwards compatible: older plugin versions keep working against a newer script, so several NetBox instances sharing one PSU server don't have to upgrade together.

## Things to know

- **Reverse proxies and gateways.** The sync fetches leases, reservations, exclusions and options for a whole server in one request each. If PSU sits behind a proxy or gateway, make sure its response size limit is large enough for your biggest server.
- **Failover pairs.** NetBox makes changes on the primary server only, then asks it to replicate to the partner. PSU's service account needs DHCP administrator rights on both servers for this (see [Installation](installation.md#failover-pairs-run-psu-as-a-dhcp-administrator)).
- **A failed read is an error, never an empty result.** If Windows can't read something, the endpoint returns an error, and the plugin skips that step rather than acting on missing data.

## Endpoint reference

All paths are under `https://<server>:<port>`. `scope_id` is always the scope's network address (for example `10.0.1.0`). Client IDs use Windows's `00-11-22-33-44-55` format; the reservation endpoints accept any MAC format and convert it. An invalid IP address in any field returns 400 naming the field.

### Health and monitoring

| Method | Path | Role | Description |
| --- | --- | --- | --- |
| GET | `/api/dhcp/health` | Reader | Returns `{"status":"ok","version":"x.y.z"}` |
| POST | `/api/dhcp/health` | Writer | Tests write access; changes nothing |
| GET | `/api/dhcp/metrics` | Reader | A health snapshot for monitoring tools: server statistics, per-scope utilization, failover state and database health. Optional `?reservations=true` splits reservations into active and inactive; `?declined=true` adds each scope's bad-address count. Used by the [LibreNMS Windows DHCP plugin](https://github.com/averyhabbott/librenms-windows-dhcp). |

### Scopes

| Method | Path | Role | Description |
| --- | --- | --- | --- |
| GET | `/api/dhcp/scopes` | Reader | Every scope, with `router` and `failover_name`. `?include_router=false` leaves out `router` (faster; the plugin reads the router with the options). `?active_only=true` leaves out inactive scopes (kept for older plugins). Fails if the failover read fails. |
| GET | `/api/dhcp/scopes/:scope_id` | Reader | One scope (without `router` and `failover_name`) |
| POST | `/api/dhcp/scopes` | Writer | Create a scope. Body: `scope_id`, `name`, `start_ip`, `end_ip`, `subnet_mask`, plus optional `description`, `lease_duration_seconds`, `router`, `state` |
| PUT | `/api/dhcp/scopes/:scope_id` | Writer | Update a scope. Any of `name`, `description`, `start_ip`, `end_ip`, `lease_duration_seconds`, `router`, `state`, `options`, and `failover` (join or leave a failover) |
| DELETE | `/api/dhcp/scopes/:scope_id` | Writer | Delete a scope. Returns 204. |

`state` is `Active` or `InActive` (any capitalization); anything else is a 400.

### Leases and reservations

| Method | Path | Role | Description |
| --- | --- | --- | --- |
| GET | `/api/dhcp/leases` | Reader | Active leases (including reservations in use). `?scope_id=` for one scope; `?format=grouped` returns them keyed by scope. |
| GET | `/api/dhcp/reservations` | Reader | Reservations. Same options as leases. |
| POST | `/api/dhcp/reservations` | Writer | Create one reservation (a single object; returns 201), or many (a list; see [Bulk reservations](#bulk-reservations)) |
| PUT | `/api/dhcp/reservations` | Writer | Update reservations found by scope and IP (a list) |
| DELETE | `/api/dhcp/reservations` | Writer | Delete reservations found by scope and IP (a list) |

### Failovers

| Method | Path | Role | Description |
| --- | --- | --- | --- |
| GET | `/api/dhcp/failover` | Reader | Failover relationships |
| POST | `/api/dhcp/failover` | Writer | Create a failover relationship (run against the primary) |
| POST | `/api/dhcp/failover/replicate` | Writer | Replicate the given scopes to the partner. Body: `{"scope_ids": [...]}`; returns `{"replicated": [...]}` |

### Options and exclusions

| Method | Path | Role | Description |
| --- | --- | --- | --- |
| GET | `/api/dhcp/options` | Reader | Every scope's option values in one call: `{"scope_options": {scope_id: [...]}, ...}` |
| GET | `/api/dhcp/options/server` | Reader | Server-level option values |
| GET | `/api/dhcp/options/scope/:scope_id` | Reader | One scope's option values |
| GET | `/api/dhcp/exclusions` | Reader | With `?scope_id=`: that scope's exclusion ranges. Without it: every scope's, keyed by scope ID (scopes with none are left out). |
| POST | `/api/dhcp/exclusions` | Writer | Add an exclusion range. Body: `scope_id`, `start_ip`, `end_ip` |
| DELETE | `/api/dhcp/exclusions` | Writer | Remove an exclusion range. Body: `scope_id`, `start_ip`, `end_ip` (all three required) |

### Response shapes

**Scope**

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

`router` is `null` when the scope has no router option; `failover_name` is `null` for a standalone scope.

**Lease** (only `Active` and `ActiveReservation` leases are returned)

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

**Reservation**

```json
{
  "ip_address": "10.0.1.100",
  "client_id": "00-11-22-33-44-55",
  "name": "printer-01",
  "description": "",
  "type": "Both",
  "scope_id": "10.0.1.0"
}
```

**Failover**

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

`primary_server` is the name of the server you asked. `state_switchover_interval` is `null` when automatic switchover is off.

**Option value** (`value` is always a list)

```json
{
  "code": 6,
  "name": "DNS Servers",
  "value": ["8.8.8.8", "8.8.4.4"],
  "type": "IPv4Address",
  "vendor_class": ""
}
```

### Bulk reservations

`POST`, `PUT` and `DELETE /api/dhcp/reservations` take a JSON list and apply each item on its own. Windows has no all-or-nothing option, so one bad item never stops the rest.

| Method | Required | Optional |
| --- | --- | --- |
| POST | `scope_id`, `ip_address`, `client_id` | `name`, `description`, `type` (`Dhcp`, `Bootp` or `Both`) |
| PUT | `scope_id`, `ip_address` | `client_id`, `name`, `description`, `type`. Only the keys sent are changed; `""` clears `name` or `description`. |
| DELETE | `scope_id`, `ip_address` | none |

When `POST` creates a reservation for an IP that has an active lease held by the same client, it also marks that lease an active reservation, as the DHCP snap-in does. Otherwise Windows leaves it an inactive reservation. If that step fails, the reservation is still created and the failure is logged as a Warning.

The response is 200 with one result per item, in order:

```json
{
  "results": [
    {"scope_id": "10.0.1.0", "ip_address": "10.0.1.100", "status": "ok",
     "reservation": {"ip_address": "10.0.1.100", "client_id": "00-11-22-33-44-55", "name": "printer-01",
                     "description": "", "type": "Both", "scope_id": "10.0.1.0"}},
    {"scope_id": "10.0.1.0", "ip_address": "10.0.1.101", "status": "not_found",
     "error": "No reservation at 10.0.1.101 in scope 10.0.1.0."}
  ]
}
```

- **`status`** is `ok`, `not_found` (no reservation at that IP in that scope) or `error` (with a message). An `ok` from POST or PUT includes the reservation as the server now has it.
- **`scope_id` is a safety check:** a reservation is only changed when it's in the scope named.
- **Changing `client_id`:** a client ID must be unique within a scope, so a change to one another reservation already holds is refused. Items run in order, so a batch can move a client ID from one reservation to another once the first has let it go, but two reservations can't swap directly. If a change fails and the reservation is lost, the endpoint re-adds the original and says so.
- A body that isn't a list gets a 400. The plugin sends at most 100 items per request.

## Inside the script

PSU runs each endpoint on its own, so functions from one endpoint aren't visible to another. The script keeps its shared helpers in one string (`$H`) and puts it in front of every endpoint's code, so each endpoint is self-contained.

Two PowerShell habits the script relies on, for anyone editing it:

- Lists are written with `ConvertTo-Json -InputObject $result`, not `$result | ConvertTo-Json`, so a one-item list stays a list.
- A helper that writes an error response is called as its own statement, then `return`. Called inside an `if` condition, its response is swallowed as the condition's value and never reaches the caller.
