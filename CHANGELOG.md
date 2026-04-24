# Changelog

All notable changes to this project will be documented in this file.

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
- PowerShell Universal v5 API script (`psu/dhcp_api_endpoints.ps1`)
