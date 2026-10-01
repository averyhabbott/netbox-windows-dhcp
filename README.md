# netbox-windows-dhcp

A NetBox plugin that connects NetBox to Windows DHCP Server through [PowerShell Universal](https://docs.devolutions.net/powershell-universal/) (PSU). It keeps DHCP scopes, options, exclusions, reservations and leases in step between NetBox and your DHCP servers, in whichever direction you choose.

## What it does

- **Brings your DHCP servers into NetBox.** Import failovers, scopes, option values and exclusion ranges from a live server, then keep them up to date with a scheduled sync.
- **Tracks leases and reservations as NetBox IP Addresses**, with status, DNS name, client MAC and lease details, and cleans up the ones the server no longer has.
- **Lets you choose the source of truth.** With the push settings on, NetBox wins and changes are pushed to the DHCP servers, most of them right away. With them off, the DHCP servers win and NetBox mirrors them.
- **Handles failover pairs:** changes go to the primary and are replicated to the partner. If the primary is down, the sync reads through the secondary.
- **Protects what you choose:** a tag keeps IPs (or whole prefixes) out of the sync's reach, and maintenance mode pauses a server, failover or scope.
- **Keeps you out of trouble:** edits the next sync would undo are refused, and a failed read from a server never deletes anything.
- **Runs everything as background jobs,** recorded in NetBox's changelog and able to fire NetBox event rules.
- **Full REST API** for all plugin objects.

## Requirements

NetBox 4.5 or 4.6, Python 3.12 or later, and a licensed PowerShell Universal install on each Windows DHCP server. See [Requirements](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/docs/requirements.md) for the details.

## Quick start

```bash
pip install netbox-windows-dhcp
```

Add `'netbox_windows_dhcp'` to `PLUGINS` in `configuration.py`, run `python manage.py migrate`, and restart NetBox and its RQ workers. Then set up PSU on each DHCP server and import your first server; the [Installation guide](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/docs/installation.md) walks through every step.

## Documentation

| Guide | What's in it |
| --- | --- |
| [Requirements](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/docs/requirements.md) | Software versions and licenses |
| [Installation](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/docs/installation.md) | From a running NetBox and PSU to your first imported DHCP server |
| [Upgrading](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/docs/upgrading.md) | Upgrading the plugin and the PSU scripts |
| [Configuration](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/docs/configuration.md) | The Settings and Schedule pages, and `PLUGINS_CONFIG` overrides |
| [How the sync works](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/docs/sync-logic.md) | Who wins for what, how scopes and IPs are matched, and the safety rules |
| [Editing rules](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/docs/editing-rules.md) | What you can create, edit and delete, and when |
| [Operations](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/docs/operations.md) | Maintenance mode, protected IPs, Unassigned Scopes, commands and troubleshooting |
| [REST API](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/docs/api.md) | The plugin's NetBox API |
| [PowerShell Universal](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/docs/psu.md) | How the plugin uses PSU, and the PSU endpoint reference |
| [Changelog](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/CHANGELOG.md) | What changed in each release |

## Testing

The plugin ships an offline Django test suite (no DHCP server, PSU or network needed). Run it from the NetBox install directory:

```bash
python manage.py test netbox_windows_dhcp --keepdb
```

## License

Released under the GNU General Public License v3.0 (or later). See [LICENSE](https://github.com/averyhabbott/netbox-windows-dhcp/blob/main/LICENSE).
