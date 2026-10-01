# Editing rules

The plugin refuses edits that the next sync would undo or delete, and edits to things Windows owns. The rules are the same in the UI, bulk edit, bulk import and the REST API. The sync's own writes aren't subject to them.

Every refusal says why. In the API, a blocked create returns **403**, and a change to a field you can't edit returns **400** naming the field. Sending a field's current value unchanged is always fine.

## Scopes

| | Push Scope Info **off** (the server owns scopes) | Push Scope Info **on** (NetBox owns scopes) |
| --- | --- | --- |
| Create | ❌ Blocked. Scopes come from Import and the sync. | ✅ |
| Edit | ⚠️ The edit page opens with a banner. Only **prefix**, **tags** and **custom fields** can change (plus maintenance mode and notes, from their own buttons or the API). Everything else is read-only. | ✅ Everything |
| Bulk edit | ❌ Blocked | ✅ |
| Delete | ✅ NetBox only. The server is untouched, and the next sync imports the scope again if it's still there. | ✅ Also deletes it from the server, with its leases and reservations |

Linking a prefix (for example to an Unassigned Scope) works in both modes, as long as the prefix matches the scope's network.

Also, in both modes:

- **One scope per prefix,** and one scope per network on each server (counting its standalone scopes and every failover it's in).
- **The range guard:** a change to a scope's start or end is refused if it would bring into the range IPs the sync would then delete (see [IP Addresses](#ip-addresses)). The error lists up to five of them.
- **Duplicate option codes** on one scope are refused.

## Exclusion ranges

| | Push Scope Info **off** | Push Scope Info **on** |
| --- | --- | --- |
| Create | ❌ Blocked | ✅ Pushed to the server right away |
| Edit | ⚠️ Only **description**, **tags** and **custom fields**. Start, end and scope are read-only. | ✅ Pushed right away |
| Delete (single or bulk) | ✅ NetBox only | ✅ Removed from the server right away |

In both modes, **the range guard** applies. Changing or deleting an exclusion so that its IPs rejoin the scope's range is refused if those IPs aren't allowed there. Deleting the whole scope (which takes its exclusions with it) isn't blocked.

## Option values

| | Push Scope Info **off** | Push Scope Info **on** |
| --- | --- | --- |
| Create | ❌ Blocked | ✅ |
| Edit | ❌ Blocked | ✅ |
| Delete | ✅ If no scope uses it | ✅ If no scope uses it |

**An option value that any scope still uses can't be deleted,** in either mode; the error says how many scopes use it. With Push Scope Info on, deleting a shared value would silently strip it from every scope on the server. Remove it from the scopes first, then delete it.

## Option code definitions

The built-in Windows option codes can't be deleted, and **Built-in** can't be set or cleared (in the API, `is_builtin` is read-only). Codes you add can be edited and deleted.

## Failovers

| Action | Allowed |
| --- | --- |
| Create | ❌ Never. Failovers come from **Import from Server**. |
| Edit | ⚠️ Only **Default Scope VRF**, **description**, **tags** and **custom fields** on the edit page. **Sync Enabled** and maintenance mode have their own buttons (the API can change them too). The Windows settings are shown read-only, and the shared secret isn't shown at all. |
| Delete | ✅ |

## DHCP servers

Servers can be created, edited and deleted freely. The **App Token** field shows blank when editing; leave it blank to keep the stored token, or enter a new one to replace it. NetBox's changelog shows the token as `********`, never the token itself.

## IP addresses

These rules apply only while **Sync IP Addresses from Leases & Reservations** is on. With it off, the sync never cleans up IPs, so nothing is locked.

**Inside a scope's start–end range, outside its exclusions** (the part of the scope the server hands out), in the scope prefix's VRF, you can only create or edit an IP the sync would keep:

| Push Reservations | Allowed inside the range |
| --- | --- |
| Off | Only IPs the sync already manages, keeping their address and VRF |
| On | Those, plus any IP with the DHCP Reservation Status |

**Protected IPs are exempt.** An IP counts as protected if it carries the Sync-Protected Tag (including a tag added in the same edit) or sits inside a prefix that carries it. The error message names the tag.

**Anywhere else:** an IP with the DHCP Lease Status must sit inside a DHCP scope's prefix in the same VRF, and outside that scope's exclusions. Other statuses aren't restricted.

Scopes without a prefix don't lock anything.

## Permissions

The plugin's buttons follow NetBox's permissions, including per-object limits:

| Button | Needs |
| --- | --- |
| Sync Now, Import from Server, Update PSU Scripts, maintenance mode, certificate import/remove, Test Connection | Change permission on that server |
| Failover Sync Enabled, maintenance mode | Change permission on that failover |
| Scope maintenance mode | Change permission on that scope |
| Fetch Certificate (on the add/edit form) | Add or change permission on DHCP servers |
| The Schedule page (**Schedule**, **Run Now**) and the Settings page | Superuser |

Bulk actions only act on the objects you may change, and say how many they skipped. The **Current Maintenance** page lists only what you may view.
