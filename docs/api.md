# REST API

The plugin adds NetBox REST API endpoints under `/api/plugins/windows-dhcp/`. They work like NetBox's own: token authentication, NetBox permissions (including per-object limits), filtering, bulk operations, and `?brief=true`.

This page covers the plugin's NetBox API. The API on each DHCP server, which NetBox calls, is described in [PowerShell Universal](psu.md#endpoint-reference).

| Endpoint | Objects |
| --- | --- |
| `/servers/` | DHCP servers |
| `/failover/` | Failover relationships |
| `/scopes/` | DHCP scopes (with their exclusion ranges nested) |
| `/exclusion-ranges/` | Exclusion ranges |
| `/option-values/` | Option values |
| `/option-codes/` | Option code definitions |
| `/lease-info/` | Lease details for IP Addresses (read-only) |

**Turning the API off:** with **API Enabled** off on the Settings page, every endpoint returns 503 Service Unavailable.

**Related objects** are returned nested and written by ID with the `_id` form: `prefix_id`, `server_id`, `failover_id`, `scope_id`, `option_definition_id`, `default_scope_vrf_id`, `option_value_ids`.

**The editing rules apply.** Everything in [Editing rules](editing-rules.md) is enforced here too. A blocked create returns **403**. A change to a field you can't edit returns **400** naming the field; sending its current value unchanged is fine.

## Servers

Fields: `name`, `hostname`, `port`, `use_https`, `api_key` (write-only; never returned), `verify_ssl`, `sync_standalone_scopes`, `default_scope_vrf`.

Read-only fields, kept up to date by the health check and sync:

| Field | Meaning |
| --- | --- |
| `health_status` | `healthy`, `unreachable` or `unknown` |
| `access_level` | Whether the token can write: `rw`, `ro` or `unknown` (the Writable column) |
| `last_health_check`, `health_error` | When the last check ran, and its error |
| `last_sync_at`, `last_sync_error` | When the server last synced, and its error |
| `psu_script_version` | The PSU script version the server reported |
| `has_ca_cert`, `ca_cert_expiry` | Whether a trusted certificate is stored, and when it expires (the certificate itself isn't exposed) |

Filters include `health_status` (e.g. `?health_status=unreachable`), `access_level` (e.g. `?access_level=ro`) and `maintenance_mode`.

## Failovers

Failovers come from **Import from Server**, so `POST` returns 403. Of the fields, only these can change: `description`, `default_scope_vrf`, `sync_enabled`, `maintenance_mode`, `maintenance_notes`, `tags` and `custom_fields`. The Windows settings (`primary_server`, `secondary_server`, `mode`, the timers, `enable_auth`) are read-only. The shared secret isn't part of the API.

Filters include `sync_enabled` and `maintenance_mode`. `DELETE` works as normal.

## Scopes

| Field | Notes |
| --- | --- |
| `name`, `description`, `start_ip`, `end_ip`, `router` | |
| `lease_lifetime` | Seconds |
| `active` | Whether the scope is active on the server |
| `prefix` | Can be `null` (a scope without a prefix) |
| `network`, `prefix_length` | Always filled in. Copied from the prefix when there is one; required when there isn't. |
| `server` or `failover` | Exactly one: a standalone scope's server, or its failover |
| `option_values` | Written as `option_value_ids`. Two values with the same option code are refused. |
| `exclusion_ranges` | Read-only here; manage them at `/exclusion-ranges/` |

With **Push Scope Info off**, only `prefix`, `maintenance_mode`, `maintenance_notes`, `tags` and `custom_fields` can change; creating a scope returns 403, and `DELETE` removes it from NetBox only. With it on, saves and deletes are pushed to the DHCP server right away.

Filters include `network`, `prefix_length`, `has_prefix` (`?has_prefix=false` lists Unassigned Scopes), `active`, `server_id`, `failover_id`, `site`, `location`, `vrf`, `within_prefix` and `maintenance_mode`.

## Exclusion ranges

Fields: `scope`, `start_ip`, `end_ip`, `description` (NetBox only). With Push Scope Info off, creating returns 403 and only `description`, `tags` and `custom_fields` can change. Changes that would bring disallowed IPs into a scope's range are refused with 400 (see [Editing rules](editing-rules.md#exclusion-ranges)).

## Option values and codes

- **Option values** (`option_definition`, `value`, `friendly_name`): with Push Scope Info off, creating and editing return 403. A value any scope still uses can't be deleted.
- **Option codes** (`code`, `name`, `data_type`, `description`, `vendor_class`, `is_builtin`): `is_builtin` is read-only, and built-in codes can't be deleted (409).

## Maintenance mode

Servers, failovers and scopes have `maintenance_mode` and `maintenance_notes`, which you can change, plus `maintenance_enabled_at` and `maintenance_enabled_by`, which are filled in automatically when maintenance is turned on. Turning it off clears the notes, who and when.

## Lease info

`/lease-info/` is read-only. Each entry is one IP Address's DHCP details: `ip_address` (brief), `lease_hostname` (the raw hostname the client sent), `active` (the server has a lease on it right now) and `lease_expiration`.

Filters: `ip_address_id`, `address`, `active`, `lease_hostname`. You only see entries for IP Addresses you're allowed to view.

## Not in the API

These stay UI-only, because they act on live DHCP servers or the plugin's settings: **Sync Now**, **Run Now** and **Schedule**, **Import from Server**, **Update PSU Scripts**, certificate import and removal, and the Settings page.

## Changes made by the plugin

The sync's changes appear in NetBox's changelog as `DHCP-Sync-Service` and fire event rules like any other change, so webhooks on IP Addresses see every IP the sync creates, updates or deletes.
