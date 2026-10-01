# Installation

This guide starts from:

- a working NetBox install (see [Requirements](requirements.md) for versions), and
- a Windows DHCP server with PowerShell Universal (PSU) installed and running.

It ends with your first DHCP server imported into NetBox. Upgrading an existing install? See [Upgrading](upgrading.md) instead.

## 1. Install the plugin in NetBox

On the NetBox host, in NetBox's Python environment:

```bash
pip install netbox-windows-dhcp
```

To keep the plugin across NetBox upgrades, also add `netbox-windows-dhcp` to NetBox's `local_requirements.txt`.

Add the plugin to `configuration.py`:

```python
PLUGINS = [
    'netbox_windows_dhcp',
]
```

No `PLUGINS_CONFIG` entry is needed. Settings live on the plugin's Settings page (the optional overrides are in [Configuration](configuration.md#plugins_config-overrides)).

Run the migrations, then restart NetBox and its RQ workers:

```bash
python manage.py migrate
sudo systemctl restart netbox netbox-rq
```

`migrate` also creates:

- the `dhcp_client_id` custom field on IP Addresses (the client MAC);
- the `invalid-client-hostname` tag;
- the `DHCP-Sync-Service` user, which the sync's changes are recorded under;
- an **Unassigned Scopes** saved filter on the Scopes list.

A **Windows DHCP** menu now appears in NetBox's sidebar.

## 2. Prepare PSU on each DHCP server

Do this on every DHCP server you want NetBox to manage, including both members of a failover pair.

### License PSU

Apply your PSU license. The plugin needs App Tokens and Permissions, which are licensed PSU features (see [Requirements](requirements.md#powershell-universal)).

### Open the firewall

PSU listens on the port chosen during its install, but Windows Firewall blocks it until you add a rule. From an elevated PowerShell session (change the port to match yours):

```powershell
New-NetFirewallRule -Name "PSU HTTPS" -DisplayName "PSU HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
```

### Choose an HTTPS certificate

PSU creates a self-signed certificate when it installs. Pick one:

- **Keep the self-signed certificate.** NetBox stores and trusts it when you add the server (step 3). Nothing to do here.
- **Use a certificate from your CA** (recommended for production). Import it into the Local Machine certificate store, then point PSU at it in `C:\ProgramData\PowerShellUniversal\appsettings.json`:

  ```json
  "Kestrel": {
    "Endpoints": {
      "Https": {
        "Url": "https://*:443",
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

  Restart PSU afterwards: `Restart-Service -Name "PowerShellUniversal"`.

### Failover pairs: run PSU as a DHCP administrator

If a server is in a failover relationship and NetBox will push to it (Push Scope Info or Push Reservations on), the account PSU's service runs as must be in the **DHCP Administrators** group on **both** servers in the pair. PSU runs as Local System by default, which can't make changes on the partner. See [Running as a Service Account](https://docs.devolutions.net/powershell-universal/config/running-as-a-service-account) to change it, and [`Add-DhcpServerSecurityGroup`](https://learn.microsoft.com/en-us/powershell/module/dhcpserver/add-dhcpserversecuritygroup) if the group doesn't exist yet.

> **If PSU won't start after changing its account** (Event ID 7000/7009, Error 1053) and the Application log shows `SqliteException ... attempt to write a readonly database`, give the account Modify rights on PSU's data folder:
>
> ```powershell
> icacls 'C:\ProgramData\UniversalAutomation' /grant 'DOMAIN\svc-account:(OI)(CI)M' /T
> ```

### Create the roles and App Tokens

The plugin uses two PSU roles:

| Role | Can do | Token created |
| --- | --- | --- |
| `DHCPReader` | Read only | `NetBox-DHCP-Read` |
| `DHCPWriter` | Everything, including **Update PSU Scripts** | `NetBox-DHCP-Write` |

`psu/setup_roles.ps1` (in this repository) creates both roles and one token for each. It needs a temporary administrator token to sign in:

1. In the PSU admin console, go to **Security → Tokens → Create Application Token**.
2. Tick **System Identity**, give it a name (for example `bootstrap`), and set **Role** to `Administrator`. A short expiry is fine.
3. Run the script from the DHCP server, or any machine with PowerShell 5.1+ that can reach PSU:

   ```powershell
   .\setup_roles.ps1 -BaseUrl https://dhcp01.example.com:443 -AdminToken eyJ...
   ```

4. **Copy both token values now.** They're shown only once.

Tokens expire after 365 days by default; add `-LifespanDays 730` (or another number) to change that. The script is safe to run again: it skips roles and tokens that already exist.

<details>
<summary>Creating the roles by hand instead</summary>

1. **Security → Roles:** create `DHCPReader` and `DHCPWriter`. Give `DHCPWriter` the `apis/*` permission, which **Update PSU Scripts** needs.
2. **Security → Tokens → Create Application Token**, once per role: tick **System Identity**, name it, set **Role**, and copy the value.

</details>

## 3. Add the server in NetBox

Go to **Windows DHCP → Infrastructure → Servers** and click **Add**.

| Field | What to enter |
| --- | --- |
| Name | Any name for the server in NetBox |
| Hostname | The server's **fully qualified name, as Windows DHCP knows it** (for example `dhcp01.corp.example.com`). Import matches failover partners to NetBox servers by this name. |
| Port | PSU's port |
| Use HTTPS | On |
| App Token | `NetBox-DHCP-Write`. A NetBox that should only ever read this server can use `NetBox-DHCP-Read`, but then the server always wins in the sync (see [How the sync works](sync-logic.md#read-only-api-keys)), and Update PSU Scripts has to run from a NetBox with a writer token. |
| Verify SSL Certificate | On. With a self-signed certificate, click **Fetch Certificate**, check the SHA-256 fingerprint against the server's certificate, then click **Trust This Certificate**. |
| Sync Standalone Scopes | On if this server has scopes that aren't in a failover |
| Default Scope VRF | The VRF that scopes learned from this server belong in. Blank means Global. |

Click **Test Connection** to check that NetBox can reach PSU and whether the token can write, then **Save**.

**For a failover pair, add both servers before importing.** Import skips a failover whose partner isn't in NetBox.

## 4. Install the endpoints with Update PSU Scripts

On the server's page, click **Update PSU Scripts**. NetBox sends the plugin's PSU endpoints to the server, then runs a health check. The job log should end with `PSU scripts updated to v2.0.0 ✓`. Repeat for each server (or select them all on the Servers list and use the bulk action).

The Servers list should now show the server as **Healthy**, with a green check next to the script version.

## 5. Review the settings before importing

Go to **Windows DHCP → Admin → Settings**. The ones that matter for a first import:

- **Push Scope Info** and **Push Reservations:** leave both **off** for the first import and sync, so nothing is written to the DHCP server until you've checked what NetBox learned.
- **Create Missing Prefixes on Import:** on (the default) creates a NetBox prefix for any scope that doesn't have one, in the Default Scope VRF. Off imports those scopes without a prefix instead (they appear under Unassigned Scopes).
- **Sync IP Addresses from Leases & Reservations:** off by default. Turn it on when you want leases and reservations to become NetBox IP Addresses.
- **Sync-Protected Tag:** pick the tag that keeps IPs away from the sync, and tag anything you don't want the sync to touch **before** the first sync with IP sync on.
- **DHCP Lease Status** and **DHCP Reservation Status:** the IP Address statuses the sync uses.

[Configuration](configuration.md) describes every setting.

## 6. Import from the server

On the server's page, click **Import from Server**. The job brings in the server's failovers, scopes, scope option values and exclusion ranges. Anything already in NetBox is skipped; the import never overwrites existing data.

**If the server is in a failover,** the first import creates the failover **in maintenance mode** and skips its scopes, so you can check where they'll go:

1. Open the failover (**Windows DHCP → Infrastructure → Failover**) and check its **Default Scope VRF**.
2. Take the failover out of maintenance mode.
3. Click **Import from Server** again. This time its scopes come in.

## 7. Check the result

- **The job log** lists what was created and skipped, with a reason for each skip or error.
- **Scopes list:** your scopes, each linked to a prefix, with its range, router and lease time.
- **Unassigned Scopes** (the saved filter on the Scopes list): scopes that were imported without a prefix, for example because the prefix already belongs to another scope. The log says why. Link a prefix by editing the scope.

Your first server is in NetBox. Next:

- Run **Sync Now** on the server, or **Run Now** on the Schedule page, and read the job log.
- Click **Schedule** on the Schedule page (**Windows DHCP → Admin → Schedule**) to start the recurring sync.
- Turn on the push settings when you want NetBox to be the source of truth. Read [How the sync works](sync-logic.md) first.
