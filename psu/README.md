# PSU DHCP API Endpoints

PowerShell Universal endpoint definitions for the `netbox-windows-dhcp` plugin.

## Prerequisites

- Windows Server with the **DHCP Server** role installed
- **PowerShell Universal v5.x** (tested on 5.6.11+) running on the DHCP server
- The `DhcpServer` PowerShell module (ships with the DHCP Server role)

### Network and HTTPS

**Windows Firewall** — During installation, PSU prompts for a port number and Kestrel binds to all network interfaces (`*:port`) by default. However, Windows Firewall will block external connections until you create an inbound rule for that port. Run the following from an elevated PowerShell session on the DHCP server (replace `<port>` with your configured port):

```powershell
New-NetFirewallRule -DisplayName "PSU HTTPS <port>" -Direction Inbound -Protocol TCP -LocalPort <port> -Action Allow
```

**HTTPS Certificate** — PSU generates a self-signed TLS certificate during installation. Two options:

*Option A — Keep the self-signed cert (simplest):* Use the plugin's **Import HTTPS Certificate** button on the DHCP Server detail page in NetBox. This stores the certificate in the database and uses it for SSL pinning without disabling SSL verification.

*Option B — Install a CA-issued cert (recommended for production):* Import the PFX into the Windows certificate store (`certlm.msc`), then update `C:\ProgramData\PowerShellUniversal\appsettings.json` to reference it by subject:

```json
"Kestrel": {
  "Endpoints": {
    "Https": {
      "Url": "https://*:<port>",
      "Certificate": {
        "Subject": "dhcp01.example.com",
        "Store": "My",
        "Location": "LocalMachine",
        "AllowInvalid": false
      }
    }
  }
}
```

Restart the PSU service after any `appsettings.json` change:

```powershell
Restart-Service -Name "PowerShellUniversal"
```

## Deployment

### Fresh install

Use this path if PSU is newly installed and you have not yet added endpoint scripts or App Tokens for this plugin.

#### 1. Open the firewall and configure HTTPS

Complete the [Network and HTTPS](#network-and-https) steps in the Prerequisites section above before continuing.

#### 2. Create roles and App Tokens

Download `setup_roles.ps1` from the [GitHub releases page](https://github.com/averyhabbott/netbox-windows-dhcp/releases) to the Windows server or any machine running PowerShell 5.1+ with HTTPS access to the PSU server.

Before running the script, create a bootstrap Administrator App Token in the PSU admin UI:

1. Go to **Security → Tokens → + Create Application Token**
2. Check **System Identity** and enter a name (e.g. `bootstrap`)
3. Set **Role** to `Administrator`
4. A short expiration is fine — this token is only needed to run the setup script once

Then run the script, replacing the hostname, port, and token:

```powershell
.\setup_roles.ps1 -BaseUrl https://dhcp01.example.com:8443 -AdminToken eyJ...
```

The script creates the `DHCPReader` and `DHCPWriter` roles and one App Token per role, then prints the token values. **Copy them immediately** — they cannot be retrieved again after the session ends.

> **Permission note:** The `DHCPWriter` role is granted `apis/*` permission, which allows the NetBox plugin to push updated endpoint scriptBlocks directly from the **Update PSU Scripts** button in the server detail page. If you are creating roles manually instead of running `setup_roles.ps1`, make sure to add this permission to the `DHCPWriter` role via **Security → Roles → Edit**.

By default tokens expire in 365 days. Override with `-LifespanDays`:

```powershell
.\setup_roles.ps1 -BaseUrl https://dhcp01.example.com:8443 -AdminToken eyJ... -LifespanDays 730
```

Prefer clicking? See [Manual setup](#manual-setup) below.

#### 3. Add the token to NetBox

In NetBox, add the DHCP Server object (**Windows DHCP → Infrastructure → Servers → Add**) and paste the appropriate token into the **App Token** field:

| Plugin configuration | Token to use |
| --- | --- |
| Sync only (Push Reservations and Push Scope Info both off) | `NetBox-DHCP-Read` (`DHCPReader` role) |
| Either push operation enabled | `NetBox-DHCP-Write` (`DHCPWriter` role) |

When in doubt, use `NetBox-DHCP-Write` — it covers all operations.

#### 4. Deploy endpoints from NetBox

On the DHCP Server detail page in NetBox, click **Update PSU Scripts**. This pushes all endpoint definitions from the plugin's bundled script directly to PSU via the management API — no file copying required.

The job log shows each endpoint created, then a health check confirming the new version is live. Wait for the job to complete before running a sync.

#### 5. Verify

Trigger **Sync Now** from the DHCP Server detail page and check the job log, or test directly:

```powershell
$headers = @{ Authorization = 'Bearer your-token-here' }
$base    = 'https://dhcp01.example.com:8443/api/dhcp'

Invoke-RestMethod -Uri "$base/health" -Headers $headers
Invoke-RestMethod -Uri "$base/scopes" -Headers $headers | ConvertTo-Json -Depth 4
```

---

### Upgrading to a new plugin version

When a new plugin version ships updated endpoint scriptBlocks:

1. `pip install --upgrade netbox-windows-dhcp` on the NetBox host
2. `python manage.py migrate` and restart NetBox + RQ workers
3. On each DHCP Server detail page, click **Update PSU Scripts** — or use **Update PSU Scripts** on the server list to bulk-update all servers at once
4. Check each job log to confirm all endpoints updated and the health check reports the new version

No file copying or PSU service restart needed.

---

### Upgrading from pre-1.2.0 (no role enforcement)

Use this path only if you are upgrading from a version of `dhcp_api_endpoints.ps1` that used a single `$_epAuth` splat with no role enforcement.

> [!IMPORTANT]
> **Existing tokens have no role assigned.** After deploying the updated script every request from a roleless token returns **401 Unauthorized**. Assign the `DHCPWriter` role to existing tokens (step 2 below) **before** clicking Update PSU Scripts.

#### 1. Assign the DHCPWriter role to existing tokens

In the PSU admin UI, go to **Security → Tokens**, edit each existing DHCP token, and set its Role to `DHCPWriter`. This restores full access without interrupting NetBox operations.

#### 2. Create scoped roles and tokens (optional)

Run `setup_roles.ps1` as described in [step 2 of the fresh install](#2-create-roles-and-app-tokens) to add the `DHCPReader` and `DHCPWriter` roles and generate new scoped tokens. The script is idempotent — if the roles already exist it skips them.

#### 3. Update the token in NetBox (optional)

If you want to restrict a sync-only NetBox instance to the read-only `DHCPReader` token, swap the App Token on that DHCP Server object now.

#### 4. Deploy updated endpoints

Click **Update PSU Scripts** on each server detail page. Check the job log to confirm all endpoints updated successfully.

---

### Manual setup

If you prefer not to run `setup_roles.ps1`, create the roles and tokens through the PSU admin UI:

1. Go to **Security → Roles** and create two roles:
   - Name: `DHCPReader` — Description: `Read-only access to DHCP API GET endpoints.`
   - Name: `DHCPWriter` — Description: `Full read/write access to all DHCP API endpoints.`
2. Go to **Security → Tokens → + Create Application Token** for each role. Check **System Identity**, enter a name, set the **Role** field, and copy each token value — shown only once.
3. In NetBox, paste the token into the **App Token** field on the DHCP Server object.

**Disabling authentication:** Authentication is enforced in the bundled endpoint script (each endpoint uses `-Authentication` and `-Role`). Disabling it is not supported — any host that can reach the PSU port would be able to read all DHCP data and make changes without credentials.

## Script Architecture

PSU v5 runs each endpoint in an isolated runspace, so functions defined in one endpoint are not available in another. The script handles this by storing shared helper functions in the `$H` string and prepending them to every endpoint's scriptblock via `[scriptblock]::Create($H + {...}.ToString())`. This makes each endpoint fully self-contained.

The script defines a `$PSUScriptVersion` constant (e.g. `'1.0.0'`) returned by `GET /api/dhcp/health`. The NetBox plugin compares this against its own `PSU_SCRIPT_VERSION` constant during health checks and shows a warning in the server list when they differ. Both constants must be kept in sync when the script changes — the plugin version and script version are independent; the script does not necessarily change with every plugin release.

Shared helpers defined in `$H`:

- `ConvertTo-ScopeObject` — maps a DHCP scope CIM object to the API response shape
- `ConvertTo-LeaseObject` — maps a lease CIM object; only returns leases with `address_state` of `Active` or `ActiveReservation`
- `ConvertTo-ReservationObject` — maps a reservation CIM object
- `ConvertTo-FailoverObject` — maps a failover CIM object; resolves the local server's FQDN for `primary_server`
- `ConvertTo-OptionValueObject` — maps a DHCP option value CIM object
- `Find-ReservationByClientId` — searches all scopes for a reservation by client MAC address
- `Write-ApiError` — returns a JSON error response with a given HTTP status code
- `Assert-ValidIPv4` — validates that a string is a valid IPv4 address; returns a 400 error response and `$false` if not

All list endpoints use `ConvertTo-Json -InputObject $result` (not `$result | ConvertTo-Json`) to prevent PowerShell's pipeline from unwrapping single-element arrays into bare objects.

URL path parameters (`:param`) and the `$Body` variable are injected automatically by PSU — no `param()` declaration is required inside endpoint scriptblocks.

## Endpoint Reference

| Method | URL | Description |
| --- | --- | --- |
| GET | `/api/dhcp/health` | Health check — returns `{"status":"ok","version":"x.y.z"}` |
| POST | `/api/dhcp/health` | Write health check — tests write access; returns `{"status":"ok"}` |
| GET | `/api/dhcp/scopes[?active_only=true]` | List all scopes (includes `router` and `failover_name`); pass `active_only=true` to exclude inactive/disabled scopes |
| GET | `/api/dhcp/scopes/:scope_id` | Get single scope by network address |
| POST | `/api/dhcp/scopes` | Create a scope |
| PUT | `/api/dhcp/scopes/:scope_id` | Update a scope |
| GET | `/api/dhcp/leases` | List active leases (optional `?scope_id=`) |
| GET | `/api/dhcp/reservations` | List reservations (optional `?scope_id=`) |
| POST | `/api/dhcp/reservations` | Create a reservation |
| PUT | `/api/dhcp/reservations/:client_id` | Update reservation by MAC |
| DELETE | `/api/dhcp/reservations/:client_id` | Delete reservation by MAC — returns 204 |
| GET | `/api/dhcp/failover` | List failover relationships |
| POST | `/api/dhcp/failover` | Create a failover relationship |
| GET | `/api/dhcp/options/server` | Server-level option values |
| GET | `/api/dhcp/options/scope/:scope_id` | Scope-level option values |
| GET | `/api/dhcp/exclusions?scope_id=` | List exclusion ranges for a scope |
| POST | `/api/dhcp/exclusions` | Create an exclusion range |
| DELETE | `/api/dhcp/exclusions` | Delete an exclusion range (body: `scope_id`, `start_ip`, `end_ip`) |

## Response Shapes

### Scope

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

`router` is read from DHCP Option 3 on the scope; `null` if not set. `failover_name` is `null` if the scope is not part of a failover relationship.

### Lease

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

Only leases with `address_state` of `Active` or `ActiveReservation` are returned.

### Reservation

```json
{
  "ip_address": "10.0.1.100",
  "client_id": "00-11-22-33-44-55",
  "name": "printer-01",
  "description": "",
  "type": "Dhcp",
  "scope_id": "10.0.1.0"
}
```

### Failover

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

`primary_server` is resolved from the local machine's FQDN. `state_switchover_interval` is `null` when automatic state switchover is disabled.

### Option Value

```json
{
  "code": 6,
  "name": "DNS Servers",
  "value": ["8.8.8.8", "8.8.4.4"],
  "type": "IPv4Address",
  "vendor_class": ""
}
```

`value` is always an array. Multi-value options (e.g. DNS server lists) contain multiple entries.

## Notes

- **`scope_id`** is always the network address of the scope (e.g. `10.0.1.0`), matching the Windows DHCP Server convention.
- **`client_id`** uses Windows DHCP hyphen-separated hex format: `00-11-22-33-44-55`. The `POST /reservations` endpoint normalises any MAC format to this convention automatically.
- **Failover creation** (`POST /api/dhcp/failover`) must be run against the **primary** server. The `Add-DhcpServerv4Failover` cmdlet contacts the secondary server directly from the primary.
- **Option 3 (Router)** is not returned in scope-level option value responses — it is included directly in the scope object as `router`. The NetBox plugin stores it on the Scope's `router` field and skips it during option value import.
- **Option 51 (Lease Time)** is also skipped during option value import — it is stored on the Scope's `lease_lifetime` field (in seconds) and displayed in the NetBox UI as the most readable exact unit (e.g. `3 Days`, `73 Hours`).
