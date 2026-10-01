# Upgrading

An upgrade has two parts: the plugin on the NetBox host, and the PSU endpoints on each DHCP server. The plugin carries the endpoint script, so upgrading the plugin gives you the new script, and **Update PSU Scripts** installs it on each server.

> **Read the release's notes in the [Changelog](../CHANGELOG.md) before you start.** Some releases have their own steps. **Upgrading to 2.0.0 from any earlier version, follow the 2.0.0 upgrade checklist there instead of the steps below:** 2.0.0's first syncs can delete data if the steps are done out of order.

## Usual steps

1. **Pause the scheduled sync** if the release notes say so: delete the scheduled **Windows DHCP Sync** job in NetBox's Jobs list. Restarting NetBox doesn't recreate it.
2. **Upgrade the plugin** in NetBox's Python environment:

   ```bash
   pip install --upgrade netbox-windows-dhcp
   ```

3. **Run the migrations and restart** NetBox and its RQ workers:

   ```bash
   python manage.py migrate
   sudo systemctl restart netbox netbox-rq
   ```

4. **Update the PSU scripts.** Click **Update PSU Scripts** on each DHCP server's page, or select them on the Servers list and use the bulk action. Check each job log: it lists the endpoints updated, created and removed, and ends with the health check (`PSU scripts updated to vX.Y.Z ✓`). Nothing needs copying to the server, and PSU doesn't need restarting.
   - This needs the `DHCPWriter` token. A server NetBox only reaches with a read-only token has to be updated from a NetBox that has a writer token.
   - Do it even when the Servers list shows no version mismatch; it's harmless when nothing changed.
5. **Check the Servers list:** every server should be **Healthy** with a green check next to its script version.
6. **Start the schedule again** if you paused it: **Schedule** on the Schedule page (**Windows DHCP → Admin → Schedule**).

## More than one NetBox on the same DHCP servers

The PSU script stays backwards compatible, so an older plugin keeps working against a newer script. When several NetBox instances share the same DHCP servers (for example production and a test copy), upgrade the PSU scripts once, from any instance with a writer token, and upgrade the other NetBox instances when you're ready.

## Upgrading from before 1.2.0

Versions before 1.2.0 had no PSU roles, so existing tokens have none. After the new script is installed, a token with no role gets **401 Unauthorized**. Before clicking **Update PSU Scripts**:

1. In the PSU admin console, **Security → Tokens**, set each existing NetBox token's role to `DHCPWriter`.
2. Optionally run `psu/setup_roles.ps1` to create the `DHCPReader` and `DHCPWriter` roles and fresh tokens (see [Installation](installation.md#create-the-roles-and-app-tokens)), and put the new token on each DHCP Server in NetBox.
