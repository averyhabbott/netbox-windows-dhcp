# Requirements

## NetBox host

| Requirement | Version |
| --- | --- |
| NetBox | 4.5.x or 4.6.x (tested on 4.5.7 and 4.6.10) |
| Python | 3.12, 3.13 or 3.14 (the versions NetBox supports) |
| Django | Comes with NetBox: 5.2 on NetBox 4.5, 6.0 on 4.6 |
| `requests` | 2.28 or later (installed with the plugin) |
| NetBox RQ workers | Must be running. Every sync, import, push and PSU script update runs as a background job. |

PostgreSQL and Redis only need to meet NetBox's own requirements.

## Each Windows DHCP server

| Requirement | Version |
| --- | --- |
| Windows Server | 2016 or later, with the **DHCP Server** role |
| `DhcpServer` PowerShell module | Ships with the DHCP Server role |
| PowerShell Universal | 5.x (tested on 5.6.11 and later), **licensed** (see below) |

## Licenses

### PowerShell Universal

**The plugin needs a licensed PowerShell Universal install on each DHCP server.** It relies on two PSU features that need a license:

- **App Tokens**, which NetBox uses to sign in to PSU.
- **Permissions**, which the plugin's `DHCPReader` and `DHCPWriter` roles use to keep read-only tokens from making changes.

Without these, the endpoints can't be secured, so an unlicensed install isn't suitable for production. PSU is licensed per running instance; see [PowerShell Universal licensing](https://docs.devolutions.net/powershell-universal/licensing) for how to buy and apply a license.

### This plugin

Released under the GNU General Public License v3.0 (or later). See [LICENSE](../LICENSE).
