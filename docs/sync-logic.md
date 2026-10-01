# How the sync works

## Who wins

Two settings decide which side is the source of truth:

| | Push Reservations **off** | Push Reservations **on** |
| --- | --- | --- |
| **Push Scope Info off** | The server wins for everything. | The server wins for scopes and leases; NetBox wins for reservations. |
| **Push Scope Info on** | NetBox wins for scopes; the server wins for leases and reservations. | NetBox wins for scopes and reservations; the server wins for leases. |

- **Leases always come from the server.**
- "Wins" means the other side is made to match. That includes **deleting** what the winning side doesn't have.
- **A read-only API key overrides both settings for that server:** it always syncs as "server wins" (see [Read-only API keys](#read-only-api-keys)).

Four safety rules apply everywhere:

1. **The Sync-Protected Tag beats the sync.** A tagged IP, or any IP inside a tagged prefix, is never changed or deleted by the sync. The tag doesn't stop pushes: it protects NetBox's data, and a push never writes to NetBox.
2. **A failed read never deletes anything.** If the server can't return its leases, reservations, exclusions or options, the steps that depend on them are skipped for that server.
3. **Maintenance mode** on a server, failover or scope means the sync and every push leave it alone.
4. **One failure doesn't stop the rest.** A problem with one IP, reservation or scope is logged, and the sync carries on.

## What a sync run does

A sync runs from the schedule, **Run Now** (every server), or **Sync Now** (one server). For each server:

1. **Health check.** Is PSU reachable, which script version does it run, and can the token write? An unreachable server is skipped.
2. **Fetch.** One call each for the server's scopes, leases, reservations, exclusions and options.
3. **For each scope NetBox and the server share:**
   1. its settings, options and exclusions are pulled or pushed;
   2. then its IP Addresses are synced (if **Sync IP Addresses** is on);
   3. then its reservations are pushed (if **Push Reservations** is on).

   Settings come first, so a range or exclusion change applies to the IP cleanup in the same run.
4. **Scopes on only one side** are created or deleted (see [Scopes on one side only](#scopes-on-one-side-only)).
5. **Replication:** failover scopes that changed are replicated to the partner, once per scope.

The job log ends with a `[TIMING]` line per server: how long each step took and how many items it processed and changed.

## Which scopes a server syncs

| Scope | Synced by | Only when |
| --- | --- | --- |
| Standalone (assigned to a server) | That server | The server's **Sync Standalone Scopes** is on |
| In a failover | The failover's **primary** | The failover's **Sync Enabled** is on |

The partner in a failover is never synced or pushed to directly; changes reach it by replication. A server with standalone sync off that isn't the primary of any failover is skipped without being contacted.

### When the primary is down

During a scheduled sync or **Run Now**, if a failover's primary is unreachable or in maintenance mode and the secondary is healthy, **the secondary stands in** for that run: the failover's scopes are synced through it, exactly as they would be through the primary. The one exception: NetBox-only scopes aren't created on it. Standalone scopes on the primary can't be reached through the secondary, so they wait. **Sync Now** on a single server never falls back.

## Scopes

### How scopes are matched

A server's scopes are matched only against NetBox scopes **tied to that server**: standalone scopes assigned to it, and scopes in a failover it belongs to. They're matched by network (the scope ID, for example `10.0.1.0`), so two servers can run the same network (say, dev and prod) with separate NetBox scopes.

- **If one server has two NetBox scopes with the same network,** the sync warns ("NetBox scopes … all use network …") and skips both, along with the server's scope, until you fix it. It never guesses.
- **Each prefix can have only one scope.**

### What syncs

Name, description, start and end, router, lease time, active state, failover membership, option values and exclusion ranges.

- **Push Scope Info off:** all of these are pulled from the server. Exclusion descriptions are NetBox-only and survive the pull.
- **Push Scope Info on:** they're pushed to the server, and options and exclusions NetBox doesn't have are removed there. The router and lease time are never removed.
- **Active state:** NetBox's **Active** field mirrors whether the scope is active on the server. With Push Scope Info on, NetBox decides, including when it creates a scope. It needs PSU script 2.0.0 to push; older scripts only pull it.
- **The router** is read together with the scope's options. If the options read fails, NetBox keeps its router rather than blanking it.

### Scopes on one side only

| | Push Scope Info **off** | Push Scope Info **on** |
| --- | --- | --- |
| **On the server, not in NetBox** | Imported into NetBox (see [Import](#import-and-auto-import)) | **Deleted from the server** |
| **In NetBox, not on the server** | **Deleted from NetBox** | Created on the server, with its reservations if Push Reservations is on |

Both directions are skipped for scopes in maintenance, failovers with sync off, servers that aren't the failover's primary, and standalone scopes when Sync Standalone Scopes is off. Inactive scopes are treated like any other.

When a scope is deleted from NetBox, by you or by the sync, **its IP Addresses stay in NetBox.**

### Import and auto-import

**Import from Server**, and the sync's auto-import with Push Scope Info off, bring in failovers, scopes, option values and exclusion ranges. They never overwrite anything already in NetBox.

- **Where a new scope goes:** its failover's **Default Scope VRF** (or its server's, for a standalone scope). The prefix is looked up only in that VRF. If there's none, one is created there when **Create Missing Prefixes on Import** is on; otherwise the scope is created without a prefix.
- **A prefix that already belongs to another scope** (or two matching prefixes in the VRF) gives a scope without a prefix. It shows under **Unassigned Scopes**, and the log says why.
- **"Already in NetBox"** means this server (or a failover it's in) already has a scope with that network, so a scope renamed on Windows isn't imported twice.
- **New failovers start in maintenance mode**, and their scopes are skipped that run. Check the failover's Default Scope VRF, take it out of maintenance, and import again.
- **A scope whose failover isn't in NetBox is skipped** with an error. Both of the failover's servers must be in NetBox first.
- **Option 3 (router) and option 51 (lease time)** are stored on the scope itself, never as option values.

### Scopes without a prefix

A scope stores its own network and prefix length, so it can exist without a NetBox prefix. It still syncs its settings, options and exclusions, but **everything IP-related is skipped:** IP sync, reservations, placeholders, the IP editing lock and prefix protection. Link a prefix later by editing the scope; the prefix must match the scope's network.

## IP Addresses

With **Sync IP Addresses from Leases & Reservations** on, each scope's leases and reservations become NetBox IP Addresses. The sync only looks at IPs **in the scope prefix's VRF**, and creates new ones there with the prefix's mask length.

### What each IP gets

| Field | From a lease | From a reservation (Push Reservations off) |
| --- | --- | --- |
| Status | DHCP Lease Status | DHCP Reservation Status |
| DNS name | The client's hostname | The reservation's name |
| `dhcp_client_id` | The client ID | The client ID |
| Description | Left alone | The reservation's description (a blank one blanks it) |
| Tenant | The prefix's tenant, for new IPs only | The prefix's tenant, for new IPs only |

- **A reservation beats a lease:** a lease never changes a reserved IP's status or DNS name. It only fills in a missing client ID.
- **DNS names** are lowercased and checked with NetBox's DNS name rules. A blank hostname blanks the DNS name. An invalid one (spaces, apostrophes, too long…) blanks it and adds the `invalid-client-hostname` tag, which comes off once the name is valid. Names are never "fixed", since a made-up name wouldn't resolve.
- **Lease details** (the raw hostname, whether it's active now, and when it expires) are kept separately and shown on the IP's page, without changelog entries. **Active** means the server has a lease on that IP right now; a reservation with no lease shows inactive, with no expiration.
- **The tenant** is only set when the sync creates an IP. It never changes an existing IP's tenant (see `dhcp_apply_prefix_tenant` in [Operations](operations.md#management-commands)).

### Which IPs survive

**Inside a scope's start–end range, outside its exclusions, the server wins:** an IP stays only if the server has a lease or reservation for it. Everything else is deleted, whatever its status, including hand-made IPs and IPs assigned to devices. With Push Reservations on, reserved IPs also stay.

**Outside the range, or inside an exclusion,** only IPs the sync manages are cleaned up. Hand-made IPs are left alone:

| NetBox IP | Server has | Result |
| --- | --- | --- |
| Lease, created by the sync | No lease | Deleted |
| Reserved, created by the sync | A lease but no reservation | Turned into a lease IP |
| Reserved, created by the sync | Neither | Deleted |
| Anything hand-made | Anything | Left alone |

With Push Reservations on, reserved IPs are never deleted or changed by cleanup, anywhere. Protected IPs never are either.

The [editing rules](editing-rules.md#ip-addresses) stop you creating IPs inside a range that the next sync would delete.

## Reservations (Push Reservations on)

NetBox wins. For each scope, every IP with the reservation status in the scope's prefix that has a client ID (inside the range or not, including inside exclusions) is made to match on the server:

| NetBox | Server reservation |
| --- | --- |
| `dhcp_client_id` | Client ID |
| DNS name | Name (compared ignoring case) |
| Description | Description |
| none | Type is always set to Both |

- **Missing on the server:** created. **Different:** updated. **On the server but not reserved in NetBox:** deleted, even inside a protected prefix.
- **A reserved IP with no client ID** leaves the server alone, unless Placeholder Reservations gives it one. If a lease shows up for that IP, the sync fills in its client ID from the lease, and the reservation is then pushed.
- **An IP with the invalid-hostname tag** doesn't blank the server's name.
- **The sync never pulls reservations into NetBox** in this mode; it only records lease details for IPs that exist.
- **Client IDs must be unique in a scope.** A second reserved IP with the same client ID is skipped with a warning. A client ID can move from one IP to another in a single run, but two IPs can't swap client IDs directly; give one a temporary client ID first.
- **A failed reservation change makes the job Failed**, not Completed, and it stays Failed on every run until it's fixed in NetBox. A delete that finds the reservation already gone is only a warning.
- **With a PSU script older than 2.0.0,** reservation push can only create reservations, or is skipped entirely. Run **Update PSU Scripts**.

### Placeholder reservations

Windows needs a client ID for a reservation, so a reserved IP with no MAC does nothing on its own: the server can still lease it out. With **Placeholder Reservations** on, such an IP (inside the scope's range, not protected) gets a made-up client ID, `ba-dc-0d-ed-` plus 8 random hex digits, unique in the scope. It's saved on the IP (with a changelog entry) and pushed. Replacing it with a real MAC updates the server. Turning the setting off leaves existing placeholders in place.

## Immediate pushes

With a push setting on, some changes go to the server right away instead of waiting for the next sync:

| You change | Setting | Job |
| --- | --- | --- |
| A scope (save) | Push Scope Info | **Windows DHCP Scope Push** (with both settings on, it also pushes the scope's reservations) |
| A scope (delete) | Push Scope Info | **Windows DHCP Scope Delete** (removes it from its failover, then deletes it) |
| An exclusion range (save or delete) | Push Scope Info | **Windows DHCP Scope Push** for its scope (both scopes if it moved) |
| A reserved IP (save, delete or status change) | Push Reservations | **Windows DHCP Reservation Push** |

- **Batched:** a bulk edit makes one job per server, and a cancelled save is never pushed.
- **Run as you:** the job is listed under the user whose change started it, and runs on the Sync Job Queue.
- **The same checks as the sync:** maintenance mode, read-only servers, failover sync off, primary only, standalone sync off.
- **A reservation push skips a scope** that isn't on the server yet, or whose reservations can't be read. The scope push brings its reservations when it creates the scope.
- **The sync's own changes never trigger pushes.** The scheduled sync still reconciles everything either way.

## Read-only API keys

A server whose token can only read (the `DHCPReader` role; **Writable** shows read-only) is always synced as "server wins", whatever the push settings say:

- Scope settings, reservations and leases are pulled.
- NetBox scopes the server doesn't have are deleted from NetBox.
- NetBox-only reserved IPs in its ranges are deleted.

Push jobs skip the server with one log line. This is intended, so a NetBox can mirror servers it must never change. It's detected by the health check at the start of each sync.

## Changelog and event rules

Every change the sync makes is recorded in NetBox's changelog as `DHCP-Sync-Service` and fires NetBox **event rules** (webhooks, scripts, notifications), just like a manual edit. With event rules on IP Addresses, expect one event per IP the sync creates, updates or deletes; a first sync of a large server can send many at once.
