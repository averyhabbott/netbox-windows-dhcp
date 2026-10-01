# Operations

Day-to-day tasks, tools and troubleshooting.

## The Windows DHCP menu

| Group | Pages |
| --- | --- |
| Infrastructure | Servers, Failover |
| Scopes | Scopes |
| Options | Option Values, Option Code Definitions |
| Admin | Current Maintenance, Schedule, Settings |

The plugin also adds a **DHCP Scopes** panel to NetBox's Prefix pages, a **DHCP Lease Info** panel to IP Address pages, and an optional **Lease Hostname** column to the IP Addresses list (add it with **Configure Table**). DHCP servers, scopes and lease info show up in NetBox's global search.

## Syncing

- **Sync Now** (on a server's page) syncs that server right away. It's refused while the server is in maintenance mode.
- **Run Now** (Schedule page) syncs every server once.
- **Schedule** (Schedule page) starts the recurring sync. See [Configuration](configuration.md#schedule-page).

Every sync is a background job; the page takes you to the job, and its log shows everything the sync did.

### Server health

Each sync starts with a health check of each server. The Servers list shows:

- **Health Status:** Healthy, Unreachable or Unknown, with the last check time and error.
- **Writable:** whether the server's token can write (read-only servers always sync as "server wins"; see [How the sync works](sync-logic.md#read-only-api-keys)).
- **Script version:** a green check when the server runs the PSU script this plugin expects. An amber warning means run **Update PSU Scripts**.

## Maintenance mode

Servers, failovers and scopes can each be put in maintenance mode. **The sync and every push leave them alone** until it's turned off:

- **A server in maintenance:** all of its scopes are skipped, and **Sync Now** is refused. For a failover, the secondary stands in if it's healthy.
- **A failover in maintenance:** all of its scopes are skipped.
- **A scope in maintenance:** just that scope is skipped.

Turn it on or off with the pause button on a list row or an object's page, or select several objects and use **Bulk Maintenance Mode**. You can add notes (a ticket number, a reason). NetBox records who turned it on and when, and clears all of that when it's turned off.

**Current Maintenance** (**Windows DHCP → Admin → Current Maintenance**) lists everything in maintenance across all three types, with **Resume** buttons to turn it off, one at a time or in bulk.

New failovers created by Import start in maintenance mode on purpose; see [Installation](installation.md#6-import-from-the-server).

## Protected IPs

Pick a tag as the **Sync-Protected Tag** on the Settings page. The sync never changes or deletes:

- an IP Address carrying the tag;
- any IP inside a prefix carrying the tag (any prefix, not just a scope's). A tag on a `/16` protects everything inside it.

Lease details (hostname, active, expiration) are still recorded for protected IPs. Protected IPs are also exempt from the [IP editing lock](editing-rules.md#ip-addresses).

The tag protects NetBox's data only. It doesn't stop a push: with Push Reservations on, a server reservation with no reserved IP in NetBox is deleted even inside a protected prefix.

**Update Client ID for Protected IPs** lets the sync update just the client ID of a protected IP from the server's active lease, for example after replacing a server. Everything else stays protected.

## Pre-staging an IP

To reserve an address for a device before you know its MAC, with **Push Reservations on**:

1. Create the IP with the DHCP Reservation Status and the planned DNS name, and leave `dhcp_client_id` blank.
2. When the device gets a lease on that address, the sync fills in `dhcp_client_id` from the lease and pushes the reservation, named after the DNS name.

With **Placeholder Reservations** on, an IP like this inside the scope's range gets a placeholder client ID straight away, so the server stops leasing the address to anything else. Enter the real MAC when you have it.

With Push Reservations off, the server owns reservations: create the reservation on the server instead.

## Unassigned Scopes

A scope with no prefix is "unassigned". It still syncs its settings, but none of its IPs (see [How the sync works](sync-logic.md#scopes-without-a-prefix)). Scopes end up unassigned when Import or the sync learns them and:

- the prefix already belongs to another scope, or
- no prefix exists in the Default Scope VRF and **Create Missing Prefixes on Import** is off.

Find them with the **Unassigned Scopes** saved filter on the Scopes list (created by `migrate`; if you delete it, the next `migrate` brings it back). The sync and import logs say why each one has no prefix. To fix one, edit the scope and pick a prefix that matches its network. This works even with Push Scope Info off.

## Lease lifetimes

Lease time is stored in seconds and shown in the largest exact unit: `86400` shows as **1 Day**, `262800` as **73 Hours**. On the scope form, enter a number and pick Seconds, Minutes, Hours or Days (new scopes default to 1 Day).

## Management commands

Run these from NetBox's install directory, in NetBox's Python environment. The plugin's two commands each take `--dry-run` to show what would change without saving anything. Their changes are recorded in the changelog as `DHCP-Sync-Service`, and protected IPs are always skipped.

### `dhcp_fix_ip_vrf`

Moves IPs the sync created with no VRF into the VRF of their scope's prefix. Older versions created every IP in Global; the sync now only looks in the prefix's VRF, so without this it would create duplicates. Run it once after upgrading from before 2.0.0, before the first sync.

```bash
python manage.py dhcp_fix_ip_vrf --dry-run
python manage.py dhcp_fix_ip_vrf
```

It only moves IPs the sync manages, keeping their history, tags and assignments. It skips (and lists) IPs whose address already exists in the target VRF, and IPs inside scope prefixes in more than one VRF.

### `dhcp_apply_prefix_tenant`

Gives IPs the sync manages the tenant of their scope's prefix. The sync only sets the tenant when it creates an IP, so use this after an upgrade or after re-tenanting a prefix.

```bash
python manage.py dhcp_apply_prefix_tenant --dry-run
python manage.py dhcp_apply_prefix_tenant              # only fills IPs with no tenant
python manage.py dhcp_apply_prefix_tenant --overwrite  # also replaces a different tenant
```

Scopes whose prefix has no tenant are skipped. It's safe to run any number of times.

### `reindex`

NetBox's own command. Run `python manage.py reindex netbox_windows_dhcp` after an upgrade so global search finds existing servers and scopes by their newest searchable fields (such as a scope's network).

## Troubleshooting

| You see | What it means, and what to do |
| --- | --- |
| A sync or push job ends **Failed**, with "N reservation change(s) failed — see log" | Some reservations couldn't be pushed. The log names each IP and PSU's reason. The job keeps failing until you fix the IP in NetBox, for example a client ID another reservation already uses. |
| Amber warning next to a server's script version | The server runs a different PSU script. Click **Update PSU Scripts**. Until you do, parts of the sync are skipped for that server. |
| **Update PSU Scripts** fails with 403 | The server's token is read-only. Update from a NetBox that uses the `DHCPWriter` token. |
| A server shows **Unreachable** | NetBox couldn't reach PSU, or the health check failed. The server's page shows the error. Check the firewall, the certificate and the token. Its scopes are skipped (a healthy secondary stands in for failover scopes). |
| "NetBox scopes … all use network …" | One server has two NetBox scopes for the same network. The sync skips both until you delete one or move it to another server. |
| "The server reports failover …, which isn't in NetBox" during import or sync | Add both of the failover's servers to NetBox, then run **Import from Server**. |
| A new failover is in maintenance mode after an import | Expected. Check its Default Scope VRF, take it out of maintenance, and import again. |
| "… is inside the DHCP range of scope …" when saving an IP | The [IP editing lock](editing-rules.md#ip-addresses): the next sync would delete that IP. Use an address outside the range, give it the reservation status (with Push Reservations on), or tag it as protected. |
| The certificate is expiring | The server's page shows a warning within 90 days of expiry. Use **Replace** to fetch and trust the new certificate. |
| A read from the server failed | The sync logs it and skips what depended on it for that server. Nothing is deleted. The next successful sync catches up. |
