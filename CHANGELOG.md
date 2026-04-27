# Changelog

All notable changes to this project will be documented in this file.

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
