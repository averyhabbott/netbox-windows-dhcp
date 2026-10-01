# Changelog

All notable changes to this project will be documented in this file.

## [2.0.0] - 2026-09-30

> ⚠️ **WARNING:** This version makes significant changes to how the plugin behaves. Do not upgrade without following the upgrade checklist below. Skipping steps can cause data loss.

2.0.0 makes the two push settings mean what they say. With **Push Scope Info** on, NetBox is the source of truth for scopes; with it off, the DHCP server is. With **Push Reservations** on, NetBox is the source of truth for reservations; with it off, the DHCP server is. Leases always come from the server. Before this release the server quietly won in several places even with push on, and cleanup left behind things the server no longer had. Because the rules are now enforced, **the first syncs after upgrading can delete data on both sides.**

### Security notice

> ⚠️ **Earlier versions exposed DHCP server App Tokens.** Every time a DHCP server was created or edited, its PSU App Token was saved in plain text in NetBox's changelog, and sent in the payload of any webhook or event rule on DHCP servers.
>
> - **Who could see them:** anyone allowed to view NetBox's changelog, in the UI or through the REST API, and any system receiving those webhooks.
> - **Fixed in 2.0.0:** the changelog and webhooks now show `********` in place of the token.
> - **Not fixed by upgrading:** changelog entries saved by earlier versions still hold the tokens, and webhook receivers may have kept copies. **Replace the App Tokens** on your DHCP servers (in PSU, then on each server in NetBox), so the exposed ones stop working.

### Requirements

- **NetBox 4.5 or 4.6.** NetBox 4.6 runs on Django 6.0.
- **Python 3.12 or later** is now required (was 3.10). pip won't install 2.0.0 on older Python.

### Upgrade checklist

Do these in order. The warnings sit on the steps they belong to.

1. **Stop the scheduled sync.** In NetBox's Jobs list, delete any scheduled **Windows DHCP Sync** jobs. After the upgrade, restarting the workers no longer creates a schedule, so it stays stopped until you start it again in step 12.
2. **Turn off Push Scope Info and Push Reservations** in the plugin's Settings, while still on the old version.
   > ⚠️ **Why this matters:** every scope already in NetBox is marked Active by the upgrade, and NetBox has no scope descriptions yet. A sync with push on, before a read-only sync, would switch on scopes that are off on the server (for example a staged migration copy) and wipe every scope description on the server.
3. **Install the new version** (`pip install --upgrade netbox-windows-dhcp`, or however you install plugins). Python 3.12 or later is required.
4. **Run `python manage.py migrate`, then restart NetBox and the RQ workers.**
5. **Click Update PSU Scripts on every DHCP server** (server page, or the bulk action on the Servers list). This needs an App Token with the DHCPWriter role. A server NetBox only reaches with a read-only token has to be updated from a NetBox that has a writer token.
   - Until a server runs PSU script 2.0.0, most of its sync is skipped (IPs, exclusions, options), reservation push is create-only, and scope active/inactive state is never pushed. The Servers list shows the version mismatch.
   - Plugin 1.3.7 keeps working against the 2.0.0 script, so another NetBox that shares the same PSU server can upgrade later.
6. **Run the one-time fixes.** The first two take `--dry-run`; check its output first.
   - `python manage.py dhcp_fix_ip_vrf`: **required.** The sync now only looks at IPs in the scope prefix's VRF. IPs created by older versions have no VRF; without this step, the sync would stop seeing them and create duplicates in the right VRF.
   - `python manage.py dhcp_apply_prefix_tenant`: *optional.* Gives existing sync-managed IPs their prefix's tenant. Run it after `dhcp_fix_ip_vrf`, since it only finds IPs in the prefix's VRF.
   - `python manage.py reindex netbox_windows_dhcp`: lets global search find scopes by network.
7. **Review the new Default Scope VRF setting** on each server and failover. Scopes the sync or Import learns from now on go into that VRF (blank means Global).
8. **Run one read-only sync** (**Run Now** on the Schedule page, or **Sync Now** on each server). With both push settings off, this sync is "server wins" everywhere:
   > ⚠️ **It deletes IPs.** Inside each scope's start/end range (outside exclusions), every IP the server doesn't have a lease or reservation for is deleted, whatever its status. That includes hand-made IPs, IPs assigned to devices, and reserved IPs that exist only in NetBox. Protected IPs (the Sync-Protected Tag on the IP or its prefix) are never touched. Tag anything you need to keep **before** this step, and check the Settings page to make sure the Sync-Protected Tag is set to the tag you use.

   It also:
   - pulls each scope's active/inactive state and description from the server;
   - overwrites the description of every reserved IP with the server reservation's description, including blank ones;
   - blanks stale DNS names (the server reports no hostname) and invalid ones (they get the `invalid-client-hostname` tag);
   - **fires your event rules** (webhooks, scripts, notifications) once per IP it creates, updates or deletes. A first sync on a large server can send many at once.
9. **Check the result before turning push back on.** Read the job log, then look for:
   - **Unassigned Scopes** (a saved filter on the Scopes list): scopes learned with no prefix. The log says why.
   - **New failovers in maintenance mode** (Import only): check their Default Scope VRF, take them out of maintenance, and run Import from Server again to bring in their scopes.
   - **"NetBox scopes … all use network …"** warnings: the sync skips both until one moves.
   - **Scopes assigned to the wrong server.** The sync now matches a server's scopes only against NetBox scopes tied to that server (standalone on it, or in a failover it belongs to). A NetBox scope pointing at the wrong server looks missing on the right one.
10. **Turn the push settings back on**, if you use them. The read-only sync in step 8 limits the risk here: by now, NetBox should match your DHCP servers.
    > ⚠️ **Push Scope Info on:** a server scope with no NetBox scope tied to that server is **deleted from Windows** on the next sync, including inactive scopes. Fix any wrong-server scopes from step 9 first.
    >
    > ⚠️ **Push Reservations on:** server reservations with no reserved IP in NetBox are **deleted from the server**, even inside protected prefixes (the Sync-Protected Tag only stops the sync writing to NetBox, never a push). Every existing reservation is changed to type Both, a one-time update per reservation.
11. **If you monitor for Failed jobs:** a sync, scope push or reservation push now ends as **Failed** when any reservation change fails, and keeps failing on every run until it's fixed in NetBox.
12. **Click Schedule** on the Schedule page (Admin → Schedule) to start the recurring sync again.
13. **Note:** if PSU sits behind a reverse proxy or gateway, check its response size limit: the sync now fetches leases, reservations, exclusions and options for a whole server in one call each.

### Sync rules (what each push setting means now)

| | Push Reservations **off** | Push Reservations **on** |
|---|---|---|
| **Push Scope Info off** | The server wins for everything. | The server wins for scopes and leases; NetBox wins for reservations. |
| **Push Scope Info on** | NetBox wins for scopes; the server wins for leases and reservations. | NetBox wins for scopes and reservations; the server wins for leases. |

- **Server wins for IPs:** inside a scope's start/end range (outside exclusions), only IPs the server has a lease or reservation for survive. Outside the range, only IPs the sync created are cleaned up; hand-made ones are left alone.
- **NetBox wins for reservations:** every reserved IP with a client ID is created or updated on the server, and server reservations NetBox doesn't have are deleted.
- **NetBox wins for scopes:** everything about the scope (name, description, range, router, lease time, active state, failover, options, exclusions) is pushed. NetBox-only scopes are created on the server; server-only scopes are deleted from it.
- **Always:** the Sync-Protected Tag wins over the sync, and a failed read from the server never deletes anything.
- **A read-only API key overrides both push settings for that server:** the server always wins, deletes included. Use this on purpose, for example a test NetBox that mirrors production DHCP without changing it.

### Added

- **Full reservation push** (Push Reservations on): creates, updates and deletes reservations on the server, and replicates them to the failover partner. Saving or deleting a reserved IP pushes right away (a new **Windows DHCP Reservation Push** job). With both push settings on, pushing a scope also pushes its reservations.
- **Placeholder Reservations** setting (off by default). A reserved IP inside a scope's range with no MAC gets a made-up client ID (`ba-dc-0d-ed-…`), so the server stops handing that IP out. Entering a real MAC later replaces it.
- **Scope active/inactive state is synced**, with a new **Active** column on the Scopes list.
- **Scope descriptions are synced.** Before, every push wiped the server's description.
- **Scopes without a prefix.** A scope stores its own network, and the prefix is optional. A scope with no prefix still syncs its settings, options and exclusions, but nothing IP-related. An **Unassigned Scopes** saved filter on the Scopes list finds them.
- **Default Scope VRF** on servers and failovers: where learned scopes go.
- **Immediate push on exclusion save and delete** (Push Scope Info on).
- **Descriptions on exclusion ranges and failovers**, and a failover **Edit** page for its NetBox-only fields.
- **Schedule page** (Admin → Schedule): **Schedule** starts the recurring sync; **Run Now** runs one sync without changing the schedule.
- **Management commands** `dhcp_fix_ip_vrf` and `dhcp_apply_prefix_tenant` (see step 6).
- **New IPs inherit their prefix's tenant** when the sync creates them.
- **`invalid-client-hostname` tag** and a **Lease Hostname** column on IP Addresses, for clients that report hostnames NetBox won't accept as DNS names.
- **Settings changes are recorded in NetBox's changelog:** who saved them, when, and what changed.
- **Sync Job Log Level** setting, a **Writable** column on the Servers list (read-only vs read-write API key), and `[TIMING]` summary lines in the sync log.

### Changed

**Sync**
- **IP cleanup follows the scope's range, not the IP's status** (see "Sync rules").
- **VRF-aware:** the sync only touches IPs in the scope prefix's VRF. Before, it could change or delete a same-address IP in another VRF.
- **Per-server scope matching:** a server's scopes are only matched against NetBox scopes tied to that server, so dev and prod can run the same network with separate NetBox scopes. Each prefix can have only one scope.
- **Inactive scopes are synced like any other** (see "Removed").
- **Safer scope handling:** creating or deleting scopes that exist on only one side now respects maintenance mode, failover sync, primary-only and Sync Standalone Scopes, both in the sync and in its auto-import.
- **The lease "Active" mark** now means the server has a lease on that IP right now. It used to stay ✓ forever.
- **DNS names use NetBox's own validation.** Invalid hostnames are blanked and tagged, never "fixed".
- **Much faster syncs:** leases, reservations, exclusions and options are fetched once per server instead of once per scope, and the router comes with the options.
- **More fail-safe:** a failed bulk read skips that step for the server, and one failing item no longer stops the rest of the job.

**Import**
- **Import uses the Default Scope VRF**, and matches existing scopes by server and network (a scope renamed on Windows isn't imported twice).
- **New failovers from Import start in maintenance mode** and their scopes are skipped that run. Check the VRF, take the failover out of maintenance and import again.
- **A scope whose failover isn't in NetBox is skipped with an error** instead of being imported as standalone.

**Editing rules**
- **IPs inside a scope's range** can only be created or edited if the sync would keep them (DHCP-managed IPs, plus reserved IPs with Push Reservations on). Protected IPs are exempt. Scope and exclusion changes that would pull disallowed IPs into a range are refused.
- **With Push Scope Info off,** you can't create scopes, exclusions or option values, but you can delete them (NetBox only; the next sync re-imports anything still on the server). Scope and exclusion edit pages open with only the NetBox-only fields editable.
- **An option value a scope still uses can't be deleted**, and duplicate option codes on one scope are refused.
- **Buttons and bulk actions honor NetBox's per-object permissions**, and the Current Maintenance page lists only what you may view.
- **Every rule applies to the REST API too.**

**Jobs and UI**
- **Saving settings never touches the sync schedule,** and worker restarts no longer create or reset it.
- **"Windows DHCP Sync" always runs as DHCP-Sync-Service.** The new **Set DHCP Sync Schedule** job shows who clicked Schedule or Run Now.
- **Push jobs are listed under the user whose change started them** (they had no user before), and run on the Sync Job Queue.
- **The health check tests write access**, and push jobs skip read-only servers quietly.
- **Update PSU Scripts ends as Failed** when a call to the server fails.
- **Settings page** is rearranged into two columns with help popups and per-field `PLUGINS_CONFIG` override notes.
- **"Add Failover" and other dead buttons removed.**

### Fixed

- **Removing an exclusion from the server never worked** (since v1.3.0).
- **PSU reads returned an empty result when Windows failed,** so the plugin could act on missing data. They now return an error, and the sync skips that step.
- **A failed failover read made every scope look standalone.**
- **Bad IP addresses sent to PSU got a generic server error** instead of a 400.
- **A scope in maintenance mode could still be auto-imported** by a pull-only sync.
- **A cancelled scope save could leave later scope pushes stuck.**
- **Sync changes never fired NetBox event rules** (see step 8).
- **"Last updated" and changelog "before" values** were wrong for changes the sync made.
- **Pushing option values could silently fail,** or report a misleading "Scope not found".
- **Scheduling bugs:** scheduled syncs showed the wrong user, "Run Now" could cancel the next scheduled sync, and worker restarts disrupted the schedule.
- **App Tokens were copied into NetBox's changelog and webhook payloads** in plain text (security fix; see [Security notice](#security-notice)).
- **Sync Now could be started just by opening a link** (security fix). It now needs the button.
- **Certificate Fetch ignored `restrict_allowlist` and `server_overrides[host].allowed`** (security fix).
- **Deleting a built-in option code** gave a server error instead of a clear "protected" message.
- **Searching option values by code number** (for example `150` in a scope's Option Values field) found nothing.

### Removed

- **Sync Active Scopes Only setting.** If you had it on, the first sync picks up the inactive scopes it used to ignore, and with Push Scope Info on, inactive server-only scopes are deleted.
- **Unique failover names.** Two server pairs can use the same failover name.

### Database migrations

- **0009:** Sync Job Log Level setting and the server's Writable field.
- **0010:** new scope fields (description, active, network, prefix length) filled in from each scope's prefix; optional scope prefix; exclusion and failover descriptions; Default Scope VRF; Placeholder Reservations; failover names no longer unique; Sync Active Scopes Only removed; created and last-updated dates on the plugin settings, for the changelog.

### REST API changes

**Could break existing API clients**
- **With Push Scope Info off:** creating scopes, exclusions or option values, and editing option values, return **403**. Changing a server-owned field on a scope or exclusion returns **400**.
- **Failovers** (`/api/plugins/windows-dhcp/failover/`): creating one returns **403**, changing a Windows-owned field returns **400**, and `shared_secret` is gone.
- **Scope `prefix` can be null.**
- **`is_builtin` on option codes is read-only.**
- **Per-object permissions** now limit what an API user can change.

**Added**
- New fields and filters for everything above: scope `description`, `active`, `network`, `prefix_length`; exclusion and failover `description`; `default_scope_vrf`; failover `sync_enabled` (now changeable).
- **Servers:** read-only status fields (`health_status`, `access_level`, `last_sync_at`, `psu_script_version` and others) and certificate status (`has_ca_cert`, `ca_cert_expiry`).
- **Maintenance mode** on servers, failovers and scopes can be read and changed.
- **New read-only `lease-info` endpoint** with each IP's lease hostname, active mark and expiration.

### PSU script 2.0.0

Installed with **Update PSU Scripts**. Existing endpoints keep their shape, except as noted.

- **Removed:** `PUT` and `DELETE /api/dhcp/reservations/:client_id` (by MAC). No plugin version ever called them; this only matters if you called them yourself.
- **Reservations:** `POST`, `PUT` and `DELETE /api/dhcp/reservations` take a list of items found by scope and IP, with a result per item. A single-object `POST` works as before.
- **Scopes:** `POST` and `PUT` take an optional `state` (`Active`/`InActive`). `GET /api/dhcp/scopes` takes an optional `include_router=false`.
- **Bulk reads:** new `GET /api/dhcp/options`; `GET /api/dhcp/exclusions` without `scope_id` returns every scope; `GET /api/dhcp/leases` and `/reservations` accept `?format=grouped`.
- **Errors:** reads return an error when Windows fails instead of an empty result, and bad IPs get a 400 naming the field.

## [1.3.7] - 2026-09-10

### Fixed

- **Duplicate/multiplying scheduled sync job chains (regression of v1.3.2/v1.3.3)** — `DHCPSyncJob` had no self-cleanup since v1.3.4, so duplicate scheduled jobs could accumulate indefinitely (hit in production: 33 concurrent overlapping syncs). Restored an advisory-locked convergence step that prunes duplicates down to one, run at the top of every sync and on settings save.
- **Scope option values never reconciled after initial import** — option values (DNS servers, domain name, etc.) were only ever synced once, at creation, so a scope could drift arbitrarily afterward without detection. Added real two-way reconciliation on every sync; requires the bundled PSU script update (v1.1.1).
- **Pushing a brand-new scope to the DHCP server always failed with a 500** — the create endpoint passed a bogus `-ScopeId` parameter to `Add-DhcpServerv4Scope`, which doesn't accept one. Removed the invalid parameter.
- **Failover relationship membership never configured on the DHCP server, and config changes never replicated to the standby** — pushing a failover-assigned scope created it as a plain standalone scope, and no config change ever propagated to the secondary. Fixed both directions: pushes now enroll/remove failover membership and trigger batched replication, and pulls keep NetBox's failover assignment in sync with the server. Requires the same PSU script update (v1.1.1).
- **Saving a DHCPScope never actually pushed it to the DHCP server**, and the intended mechanism would have triggered a full reconcile of every server on every single save. Replaced with a dedicated `DHCPScopePushJob` that pushes only the changed scope(s), batched per transaction, to only the correct server.
- **Bulk maintenance mode on the Scopes list crashed with `UnboundLocalError`** — a local re-import of `DHCPScope` shadowed the module-level one partway through the function. Removed the redundant imports.
- **"Edit Selected" and other bulk-action buttons could disappear from the Scopes/Servers/Failovers list pages** after a search, sort, or page change, because they shared a CSS class that NetBox's own htmx table refresh overwrites. Moved the plugin's custom buttons into their own container so they persist through any table interaction.
- **Deleting a `DHCPScope` in NetBox never removed it from the Windows DHCP server**, and the failover-removal cmdlet it depends on was itself broken (`Remove-DhcpServerv4Failover` doesn't take a `-ScopeId` at all). Added a `DHCPScopeDeleteJob` that deconfigures failover and deletes the scope, fixed the cmdlet bug, and fixed a live-found follow-up where the scope-update endpoint rejected a deconfigure-only request with no scope-attribute changes. Requires the same PSU script update.
- **A follow-up sweep on the scope-delete work above found several smaller gaps**: the new delete paths didn't respect maintenance-mode/sync-enabled/standalone-sync settings the way the push job does, a couple of PSU endpoints could mask real errors as fake 404s or reject a minimal request body, and some documentation had gone stale. All fixed, with new regression tests covering the eligibility checks.

### Changed

- **Clarified PSU service account setup for DHCP failover** (`psu/README.md`) — the account PSU's Windows Service runs as must be a DHCP Administrator on every server in the relationship, not just the primary, due to the Kerberos "double hop" limitation.

## [1.3.6] - 2026-06-18

### Added

- **`GET /api/dhcp/metrics` endpoint** — a read-only aggregate health snapshot for external monitoring systems: server statistics (uptime, address counts, utilization, cumulative DHCP packet counters), per-scope utilization, failover state, and database health, returned in a single call. The schema is intentionally generic (standard DHCP concepts only, no consumer-specific fields) and is consumed by the companion [LibreNMS Windows DHCP plugin](https://github.com/averyhabbott/librenms-windows-dhcp).
- **`GET /api/dhcp/metrics` per-scope detail** — each scope now reports a true `addresses_total`
  (address range minus exclusion ranges, independent of lease state) and two optional, query-gated
  lease scrapes (off by default since they enumerate leases on the DHCP server):
  `?reservations=true` splits the reserved count into `reservations_active` / `reservations_inactive`;
  `?declined=true` adds a per-scope `bad_address_count`. When both are on, the reservation pass also
  yields the declined count for scopes that have reservations, so the bad-lease scan only runs on the
  rest. Schema stays additive (`schema_version` 1).

### Changed

- PSU script version bumped to `1.1.0` for LibreNMS integration support.

---

## [1.3.5] - 2026-06-12

### Added

- **Global search for DHCP Servers and Scopes** — DHCP servers (by name and hostname) and DHCP scopes (by name and prefix CIDR) now appear in NetBox's global search, alongside the existing DHCP Lease Info results. Run `python manage.py reindex netbox_windows_dhcp` (or perform a NetBox upgrade, which reindexes lazily) to backfill existing objects into the search cache.
- **NetBox 4.6.x support** — `max_version` raised to `4.6.99`, allowing the plugin to load on NetBox 4.6.x (Django 6.0) in addition to 4.5.x. Validated against NetBox 4.5.7 and 4.6.2.
- **Test suite** — a comprehensive Django test suite now ships under `netbox_windows_dhcp/tests/`, covering models and validation, the REST API, UI views, filtersets, the sync engine, the import pipeline, the PSU HTTP client, certificate parsing, signals, and search. Runs fully offline (no DHCP/PSU server or network access required) via `python manage.py test netbox_windows_dhcp`.

### Fixed

- **DHCP Exclusion Range REST API returned HTTP 500** — the nested scope serializer was missing `brief_fields`, which broke every request to `/api/plugins/windows-dhcp/exclusion-ranges/`.
- **Creating DHCP Failover and DHCP Option Value objects via the REST API was rejected** — their nested foreign-key fields were not marked read-only, so the API required a nested object instead of accepting an ID (`*_id`). The `_id` write fields now work as intended.
- **Partial updates (PATCH) to a DHCP Scope via the REST API failed** with *"Either server_id or failover_id must be set"* when those fields were omitted from the payload. The serializer now falls back to the scope's existing server/failover.
- **DHCP Exclusion Range detail page returned HTTP 500** (`NoReverseMatch` for `dhcpexclusionrange_list`) because no list view was registered. An exclusion-range list view is now registered, fixing the detail page.

---

## [1.3.4] - 2026-05-19

### Added

- **Support for long-running tasks** — sync job timeout is now configurable in plugin settings, allowing long-running syncs to complete without being cut short.
- **DHCP scopes now import during sync, not just during a one-time import** — scopes discovered on the server are created/updated as part of regular sync operations.

### Changed

- Revised code to align better with NetBox development guidelines.

### Fixed

- Several minor bug fixes.

---

## [1.3.3] - 2026-04-28

### Fixed

- **`NameError` on every successful sync** — `_sync_server()` referenced `DHCPServer` for the `QuerySet.update()` calls added in 1.3.2 without importing it. Every server that completed a full sync raised `NameError: name 'DHCPServer' is not defined`, caught as a false "Error syncing server X" entry in the job log. `last_sync_at` and `last_sync_error` were never updated. Fixed by adding `DHCPServer` to the existing import inside `_sync_server()`.
- **Duplicate/multiplying scheduled sync job chains** — Any scheduled `DHCPSyncJob` left in the queue when a new chain started (e.g., after changing the sync interval from a large value, or after certain worker restarts) would perpetuate independently, multiplying the number of parallel chains on each restart. Fixed on two fronts: (1) `DHCPSyncJob.run()` now acquires the `job-schedules` advisory lock and cancels all other pending/scheduled sync jobs before enqueueing its successor, converging any number of chains to one on the next run; (2) `_apply_interval_to_job()` now deletes all extra pending/scheduled jobs (keeping only the earliest) when plugin settings are saved, preventing orphaned future jobs from being left in the queue after an interval change.

---

## [1.3.2] - 2026-04-27

### Added

- **Sync Active Scopes Only** — New plugin setting that instructs PSU to filter out inactive/disabled DHCP scopes before returning them to NetBox. Useful during server migrations when the replacement server has scopes in an inactive state that should not yet be managed by NetBox. PSU script updated to support `?active_only=true` query parameter on `GET /api/dhcp/scopes`. PSU script version bumped to `1.0.2`.
- **Resume from Current Maintenance** — The **Current Maintenance** screen now has per-row **Resume** buttons and a **Resume Selected** bulk action. Maintenance can be disabled for any mix of servers, failovers, and scopes directly from the dashboard without navigating to each object.

### Fixed

- **Duplicate scheduled sync jobs** — Removed `@system_job` decorator from `DHCPSyncJob`. The decorator caused the worker to enqueue a new null-user job chain on every restart, accumulating zombie entries in the jobs list. The sync chain is now entirely self-perpetuating: successor jobs are enqueued at the top of `run()` before any sync work, anchored to when the job started, with `job.interval` immediately nulled to prevent `handle()`'s built-in auto-reschedule from also firing.
- **Spurious changelog entries on every sync** — `last_sync_at` (server and scope) and `last_sync_error` (server) are now written via `QuerySet.update()` instead of `.save()`, bypassing Django's `post_save` signal entirely. Previously, every sync run generated an "Updated — No Changes" changelog entry for every scope and server touched, even when nothing actually changed. `last_health_check` on `DHCPServer` is additionally excluded from changelog diffs via `serialize_object()`.
- **Sync job user attribution** — Scheduled sync jobs in the perpetual chain now run as `DHCP-Sync-Service`. The first job in the chain (enqueued when you save the schedule) retains the triggering user's name; all successors are attributed to the service account, correctly reflecting that they are automated rather than human-initiated.
- **`DHCP-Sync-Service` account auto-created** — The plugin now creates an inactive `DHCP-Sync-Service` NetBox user account via the `post_migrate` signal. This account is used to attribute all sync-driven changelog entries, making automated changes clearly distinguishable from human edits in the audit trail.

---

## [1.3.1] - 2026-04-27

### Fixed

- `Import-Module DhcpServer -SkipEditionCheck` now only runs on PowerShell 7+ (Core edition). The `-SkipEditionCheck` parameter does not exist in Windows PowerShell 5.1 (Desktop edition) and caused a parameter error during the health check after **Update PSU Scripts** on servers running PS 5.1. Both editions now load `DhcpServer` correctly. Remote scripts version bumped to `1.0.1`.

### Changed

- Authentication is now always enforced on all PSU endpoints. Since `dhcp_api_endpoints.ps1` is bundled in the Python package (as of 1.3.0) and deployed via **Update PSU Scripts**, it can no longer be edited in place to disable authentication. An App Token is required on every server.
- Removed "Leave blank if auth is not required" hint from the App Token field on the server edit form.

---

## [1.3.0] - 2026-04-26

### Added

- **Maintenance mode** — Servers, failover relationships, and scopes can be individually placed in maintenance mode. Objects in maintenance mode are skipped entirely during sync. A stamped timestamp and user are recorded when maintenance is enabled. Maintenance notes field provides free-form context. Single-item toggle pages and bulk toggle pages available for all three object types. A **Current Maintenance** combined view (Windows DHCP → Admin → Current Maintenance) lists everything currently paused across all types.
- **Server health checks** — At the start of every sync run, each server is pinged via `GET /api/dhcp/health` before any sync work begins. Health status (`Healthy` / `Unreachable` / `Unknown`), last check time, and PSU script version are stored on the server and visible in the server list and detail view. Unreachable servers are skipped; the result is logged at the top of the job output.
- **Automatic secondary failback** — When a failover primary is unreachable or in maintenance mode and the secondary is healthy, the sync automatically routes failover-scope syncing through the secondary for that run. No configuration required. Standalone scopes are not part of the fallback.
- **PSU script version tracking** — The bundled `dhcp_api_endpoints.ps1` embeds a `$PSU_SCRIPT_VERSION` constant returned by `GET /api/dhcp/health`. The plugin compares this against its own `PSU_SCRIPT_VERSION` constant and displays a green check (match), amber warning (mismatch), or gray question mark (unknown) in the server list. Version mismatches are advisory — sync continues normally.
- **Update PSU Scripts** — New button on the server detail page (and bulk action on the server list) that pushes the bundled endpoint scriptBlocks directly to PSU via the management API. Handles first-time endpoint creation, in-place updates, new endpoint creation, and removed endpoint deletion — then restarts PSU endpoint definitions and runs a health check to confirm the new version is live. No manual file copying required.
- **PSU script bundled in package** — `dhcp_api_endpoints.ps1` is now included in the Python package (`netbox_windows_dhcp/psu/`) and read via `importlib.resources`. Works correctly for wheel installs, editable installs, and zip-imported packages.
- **Test Connection on server edit page** — AJAX button tests read and write connectivity using live form values before saving. Handles blank api_key (falls back to stored value when editing), SSL certificate errors, 401/403 responses, and network errors with explicit messages.
- **Inline cert import on server edit page** — Fetch and trust a TLS certificate directly on the server edit form without navigating away. Four-state JS widget: no cert → fetch panel (shows subject, SANs, issuer, expiry, fingerprint) → staged (trusted, not yet saved) → stored. Revert-to-saved restores the original cert if you change your mind mid-edit.
- **Sync-protect tag prefix inheritance** — The **Sync-Protected Tag** setting now applies to IP addresses that fall within any Prefix carrying the tag, in addition to IPs that carry the tag directly. A tag on a /16 protects all IPs within it, including those in nested sub-prefixes that don't carry the tag.
- **Scope filters by prefix attributes** — DHCP Scope list now supports filtering by Site, Location, VRF (from the linked prefix), and a "within prefix" CIDR range filter.
- **Plugin API enable/disable toggle** — New **API Enabled** checkbox in Plugin Settings. When unchecked, all six plugin REST API endpoints return `503 Service Unavailable`. Re-enabling restores normal operation.
- **`last_sync_at` on scopes and servers** — Timestamps recording the last successful sync run, visible in detail views.

### Changed

- **Sync Now blocked in maintenance mode** — Attempting to manually sync a server in maintenance mode is refused with a warning message. Other servers are unaffected.
- **PSU setup flow simplified** — First-time endpoint deployment no longer requires manually copying `dhcp_api_endpoints.ps1` to the PSU server filesystem. Run `setup_roles.ps1` to create roles and tokens, add the token to NetBox, then click **Update PSU Scripts**. See [psu/README.md](psu/README.md).
- **Getting Started guide updated** — Root README now references psu/README.md for the PSU setup walkthrough and reflects the new cert-import and Update PSU Scripts workflow.

---

## [1.2.1] - 2026-04-23

### Fixed

- `setup_roles.ps1` idempotency checks corrected for actual PSU API behavior: role existence is now determined by attempting `POST /api/v1/role` and treating a server error as "already exists" (PSU returns 200 with empty body for both existing and non-existing roles on the `GET` endpoint, and returns 500 on duplicate create). Revoked (soft-deleted) tokens are now filtered out of the `GET /api/v1/apptoken` response before the existence check.

---

## [1.2.0] - 2026-04-23

### Added

- **PSU role-based access control** — `dhcp_api_endpoints.ps1` now enforces two PSU roles: `DHCPReader` (GET endpoints only) and `DHCPWriter` (all endpoints). Operators can issue a read-only token to NetBox instances that only sync, and write endpoints are protected at the API layer regardless of plugin settings. Existing tokens with no role assigned will receive 401 after deploying the updated script; assign the `DHCPWriter` role to restore access.
- **`setup_roles.ps1`** — Idempotent PowerShell setup script that creates the `DHCPReader` and `DHCPWriter` PSU roles and one App Token per role via the PSU REST management API. Tokens expire in 365 days by default (configurable via `-LifespanDays`). Safe to re-run.
- **Expanded PSU README** — Covers Windows Firewall inbound rule setup, HTTPS certificate options (self-signed import vs. CA-issued via `appsettings.json`), and splits the deployment guide into fresh-install and extending-existing-install sections with correct file paths and commands.

---

## [1.1.0] - 2026-04-23

### Added

- **Configurable IP address statuses** — Plugin Settings now exposes `DHCP Lease Status` and `DHCP Reservation Status` dropdowns. Any NetBox IP Address status (including custom statuses defined via `FIELD_CHOICES`) can be used instead of the hardcoded `dhcp` and `reserved` literals. The sync, cleanup, push-reservations, and status-validation logic all follow the configured values.
- **Import HTTPS Certificate** — Admins can import a self-signed or internally CA-signed TLS certificate from a PSU server directly from the Server detail page. The certificate is stored in the database and used automatically for SSL verification, eliminating the need to disable `verify_ssl` for servers that don't have a publicly trusted certificate. The confirmation page displays the SHA-256 fingerprint for manual verification. The stored certificate panel shows expiry date and warns when expiry is within 90 days.
- **PLUGINS_CONFIG credential and behavior overrides** — `configuration.py` can now override per-server API keys via `server_overrides` and globally suppress `sync_ips_from_dhcp`, `push_reservations`, and `push_scope_info` via top-level keys. Overridden settings are applied in memory without touching the database, making it safe to share a production database replica in a development environment. The Server detail page and Plugin Settings page both show a notice when an override is active.

### Fixed

- "Expiring Soon" badge on the Stored Certificate panel was appearing for certificates more than a year away from expiry due to fragile `timeuntil` string parsing; expiry state is now computed in Python (expired / within 90 days / healthy).

---

## [1.0.1] - 2026-04-22

### Security

- PSU script now defaults to `$RequireAuthentication = $true` — authentication is required out of the box
- Sync, global sync, and import actions now require `change_dhcpserver` permission; failover toggle actions require `change_dhcpfailover` — previously any authenticated user could trigger these
- Removed GET handlers from sync views that allowed sync to be triggered without a CSRF token
- Fixed stored XSS via the `friendly_name` field on DHCPOptionValue detail page (`|safe` filter removed)
- App Token and failover shared secret are no longer rendered in edit form HTML (`render_value=False`)
- Added IPv4 format validation in PSU script before passing parameters to DHCP cmdlets
- PSU error responses no longer echo back user-supplied scope IDs

### Fixed

- Editing a DHCPServer without re-entering the App Token no longer wipes the stored token
- Editing a DHCPFailover without re-entering the Shared Secret no longer wipes the stored secret
- Replaced fragile `IPAddress.clean()` monkey-patch with a proper `post_clean` signal receiver — multiple plugins can now coexist without overwriting each other's validation

### Added

- Plugin setting **Create Missing Prefixes on Import** — controls whether importing a scope whose CIDR does not exist in NetBox automatically creates the Prefix (default: enabled, preserving prior behavior)

---

## [1.0.0] - 2026-04-12

Initial release.

- DHCP Server management (standalone and failover)
- DHCP Scope management with lease lifetime, option values, and exclusion ranges
- DHCP Failover relationship management
- DHCP Option Code Definitions and Option Values
- Background sync: pull leases and reservations from Windows DHCP Server into NetBox IP Addresses
- Push reservations from NetBox to Windows DHCP Server
- Push scope configuration from NetBox to Windows DHCP Server
- One-time import of scopes, failovers, option values, and exclusion ranges from a live DHCP server
- Sync-protected tag to prevent sync from modifying specific IP Addresses
- DHCP lease info panel injected into NetBox IP Address detail view
- DHCP scopes panel injected into NetBox Prefix detail view
- PowerShell Universal v5 API script (`dhcp_api_endpoints.ps1`)
