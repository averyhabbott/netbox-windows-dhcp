"""
Background sync job for netbox-windows-dhcp.

Sync logic:
  For each DHCPServer:
    1. Fetch /scopes from PSU.
    2. Match returned scopes against DHCPScope objects (by network address).
    3. If scope_sync_mode == 'active':
         - Fetch leases + reservations.
         - Update/create NetBox IPAddress objects with status dhcp-lease or dhcp-reserved.
         - Set dns_name from DHCP hostname.
         - Store client_id in description (custom field if configured).
    4. If push_reservations == True:
         - Push NetBox "reserved" IPs within scope ranges to DHCP server.
    5. If push_scope_info == True:
         - Push scope config to DHCP server.
"""

import logging
from typing import Optional

from core.choices import JobIntervalChoices
from netbox.jobs import JobRunner, system_job

logger = logging.getLogger('netbox_windows_dhcp')


def _load_settings():
    """Return the DHCPPluginSettings singleton from the database."""
    from .models import DHCPPluginSettings
    return DHCPPluginSettings.load()


# ---------------------------------------------------------------------------
# Module-level sync functions — shared by DHCPSyncJob and DHCPServerSyncJob
# ---------------------------------------------------------------------------

def _upsert_ip_address(job_logger, ip_str: str, prefix_len: int, status: str, dns_name: str, client_id: str):
    from ipam.models import IPAddress
    try:
        # Match on host address regardless of prefix length (/24, /32, etc.)
        qs = IPAddress.objects.filter(address__net_host=ip_str)
        if qs.exists():
            obj = qs.first()
            created = False
        else:
            obj = IPAddress(address=f'{ip_str}/{prefix_len}')
            created = True

        changed = created
        change_reasons = ['new'] if created else []

        if obj.status != status:
            if not created:
                change_reasons.append(f'status {obj.status!r}→{status!r}')
            obj.status = status
            changed = True

        new_dns = (dns_name[:255] if dns_name else '').lower()
        if new_dns and obj.dns_name != new_dns:
            if not created:
                change_reasons.append(f'dns_name {obj.dns_name!r}→{new_dns!r}')
            obj.dns_name = new_dns
            changed = True

        stored_client_id = obj.custom_field_data.get('dhcp_client_id')
        if client_id and stored_client_id != client_id:
            if not created:
                change_reasons.append(f'dhcp_client_id {stored_client_id!r}→{client_id!r}')
            obj.custom_field_data['dhcp_client_id'] = client_id
            changed = True

        if changed:
            if not created:
                obj.snapshot()
            obj.save()
            action = 'Created' if created else 'Updated'
            reason_str = f' [{", ".join(change_reasons)}]' if change_reasons else ''
            job_logger.info(f'{action} IP {ip_str}{reason_str}')
        else:
            job_logger.debug(f'IP {ip_str} already up-to-date (status={status})')
    except Exception as exc:
        job_logger.warning(f'Failed to upsert IP Address {ip_str}: {exc}', exc_info=True)


def _update_ip_addresses_from_reservations(job_logger, scope, reservations: list):
    prefix_len = scope.prefix.prefix.prefixlen
    for res in reservations:
        ip_str = res.get('ip_address') or res.get('IPAddress')
        client_id = res.get('client_id') or res.get('ClientId') or ''
        name = res.get('name') or res.get('Name') or ''
        if not ip_str:
            continue
        _upsert_ip_address(
            job_logger,
            ip_str=ip_str,
            prefix_len=prefix_len,
            status='dhcp-reserved',
            dns_name=name,
            client_id=client_id,
        )


def _update_ip_addresses_from_leases(job_logger, scope, leases: list):
    from ipam.models import IPAddress
    prefix_len = scope.prefix.prefix.prefixlen
    for lease in leases:
        ip_str = lease.get('ip_address') or lease.get('IPAddress')
        client_id = lease.get('client_id') or lease.get('ClientId') or ''
        hostname = lease.get('hostname') or lease.get('HostName') or ''

        if not ip_str:
            continue

        # Skip IPs already set to reserved (reservation takes precedence)
        try:
            existing = IPAddress.objects.get(address__net_host=ip_str)
            if existing.status == 'dhcp-reserved':
                continue
        except IPAddress.DoesNotExist:
            pass
        except Exception:
            pass

        _upsert_ip_address(
            job_logger,
            ip_str=ip_str,
            prefix_len=prefix_len,
            status='dhcp-lease',
            dns_name=hostname,
            client_id=client_id,
        )


def _push_reservations(job_logger, client, scope, scope_id: str):
    from ipam.models import IPAddress

    existing_reservations = {
        r.get('ip_address') or r.get('IPAddress'): r
        for r in client.list_reservations(scope_id=scope_id)
    }

    for ip_obj in IPAddress.objects.filter(status__in=('reserved', 'dhcp-reserved')):
        try:
            host = str(ip_obj.address.ip)
            if host not in existing_reservations:
                client.create_reservation({
                    'scope_id': scope_id,
                    'ip_address': host,
                    'client_id': '',
                    'name': ip_obj.dns_name or '',
                    'description': ip_obj.description or '',
                    'type': 'Dhcp',
                })
                job_logger.info(f'Pushed reservation {host} to server')
        except Exception as exc:
            job_logger.warning(f'Failed to push reservation {ip_obj}: {exc}')


def _push_scope(job_logger, client, scope, scope_id: Optional[str] = None):
    from netaddr import IPNetwork
    try:
        prefix_net = IPNetwork(str(scope.prefix.prefix))
        payload = {
            'scope_id': scope_id or str(prefix_net.network),
            'name': scope.name,
            'start_ip': scope.start_ip,
            'end_ip': scope.end_ip,
            'subnet_mask': str(prefix_net.netmask),
            'router': scope.router or '',
            'lease_duration_seconds': scope.lease_lifetime,
            'description': '',
        }
        if scope_id:
            client.update_scope(scope_id, payload)
            job_logger.info(f'Updated scope {scope_id} on server')
        else:
            client.create_scope(payload)
            job_logger.info(f'Created scope {payload["scope_id"]} on server')
    except Exception as exc:
        job_logger.warning(f'Failed to push scope {scope}: {exc}')


def _sync_server(job_logger, server, sync_mode: str, push_reservations: bool, push_scope_info: bool):
    from .api_client import PSUClient, PSUClientError
    from .models import DHCPScope

    job_logger.info(
        f'Syncing server: {server.name} ({server.hostname}) — '
        f'mode={sync_mode} push_reservations={push_reservations} push_scope_info={push_scope_info}'
    )
    client = PSUClient(server)

    try:
        remote_scopes = client.list_scopes()
    except PSUClientError as exc:
        job_logger.error(f'Failed to fetch scopes from {server.name}: {exc}')
        return

    # Build a map: network_address -> remote scope dict
    remote_scope_map = {}
    for rs in remote_scopes:
        scope_id = rs.get('scope_id') or rs.get('ScopeId') or rs.get('network_address')
        if scope_id:
            remote_scope_map[scope_id] = rs

    job_logger.info(
        f'Server {server.name} returned {len(remote_scope_map)} remote scope(s): '
        f'{list(remote_scope_map.keys())}'
    )

    # Build a local lookup: network address → DHCPScope
    local_scope_map = {}
    for scope in DHCPScope.objects.select_related('prefix', 'failover').prefetch_related(
        'option_values__option_definition'
    ):
        try:
            network = str(scope.prefix.prefix.network)
            local_scope_map[network] = scope
        except Exception:
            job_logger.warning(f'Could not determine network address for scope {scope} — skipping', exc_info=True)

    # Iterate remote scopes only — these are the scopes that belong to this server.
    # Looking up from the remote side means we never touch scopes from other servers.
    for scope_id, remote in remote_scope_map.items():
        scope = local_scope_map.get(scope_id)
        if scope is None:
            job_logger.info(f'Remote scope {scope_id} has no matching DHCPScope in NetBox — skipping')
            continue

        job_logger.info(f'Scope "{scope.name}" matched remote scope_id={scope_id} on {server.name}')

        if sync_mode == 'active':
            try:
                leases = client.list_leases(scope_id=scope_id)
                reservations = client.list_reservations(scope_id=scope_id)
            except PSUClientError as exc:
                job_logger.error(f'Failed to fetch leases/reservations for scope {scope_id}: {exc}')
                leases, reservations = [], []

            job_logger.info(f'Scope {scope_id}: {len(leases)} lease(s), {len(reservations)} reservation(s)')
            _update_ip_addresses_from_reservations(job_logger, scope, reservations)
            _update_ip_addresses_from_leases(job_logger, scope, leases)
        else:
            job_logger.info(f'Scope {scope_id}: skipping IP updates (sync_mode={sync_mode})')

        if push_reservations:
            try:
                _push_reservations(job_logger, client, scope, scope_id)
            except PSUClientError as exc:
                job_logger.error(f'Failed to push reservations for scope {scope_id}: {exc}')

        if push_scope_info:
            _push_scope(job_logger, client, scope, scope_id)

    # For push_scope_info: push any local scopes associated with this server that
    # don't exist remotely yet (i.e. need to be created on the DHCP server).
    if push_scope_info:
        for network, scope in local_scope_map.items():
            if network in remote_scope_map:
                continue  # already handled above
            # Only push if this scope is linked to this server via its failover relationship
            if scope.failover and (
                scope.failover.primary_server_id == server.pk
                or scope.failover.secondary_server_id == server.pk
            ):
                _push_scope(job_logger, client, scope)


# ---------------------------------------------------------------------------
# Job classes
# ---------------------------------------------------------------------------

@system_job(interval=JobIntervalChoices.INTERVAL_HOURLY)
class DHCPSyncJob(JobRunner):
    """Synchronise all DHCP servers with NetBox."""

    class Meta:
        name = 'Windows DHCP Sync'
        description = (
            'Pulls scope, lease, and reservation data from Windows DHCP servers '
            'via PowerShell Universal and updates NetBox IP Address objects.'
        )

    def run(self, *args, **kwargs):
        from .models import DHCPServer
        servers = DHCPServer.objects.all()
        if not servers.exists():
            self.logger.info('No DHCP servers configured — nothing to sync.')
            return

        cfg = _load_settings()
        sync_mode = cfg.scope_sync_mode
        push_reservations = cfg.push_reservations
        push_scope_info = cfg.push_scope_info

        self.logger.info(
            f'Starting DHCP sync: mode={sync_mode} '
            f'push_reservations={push_reservations} push_scope_info={push_scope_info}'
        )

        for server in servers:
            try:
                _sync_server(self.logger, server, sync_mode, push_reservations, push_scope_info)
            except Exception as exc:
                self.logger.error(f'Error syncing server {server.name}: {exc}')


class DHCPServerSyncJob(JobRunner):
    """Sync a single DHCPServer on demand (enqueued by the Sync Now button)."""

    class Meta:
        name = 'Windows DHCP Server Sync'
        description = 'On-demand sync for a single Windows DHCP server.'

    def run(self, *args, **kwargs):
        server_pk = kwargs.get('server_pk')
        if not server_pk:
            self.logger.error('No server_pk provided to job.')
            return

        from .models import DHCPServer
        try:
            server = DHCPServer.objects.get(pk=server_pk)
        except DHCPServer.DoesNotExist:
            self.logger.error(f'DHCPServer pk={server_pk} not found.')
            return

        cfg = _load_settings()
        _sync_server(self.logger, server, cfg.scope_sync_mode, cfg.push_reservations, cfg.push_scope_info)


class DHCPImportJob(JobRunner):
    """One-time import of failovers, scopes, and option values from a Windows DHCP server."""

    class Meta:
        name = 'Windows DHCP Import'
        description = 'One-time import of failovers, scopes, and option values from a Windows DHCP server via PSU.'

    def run(self, *args, **kwargs):
        server_pk = kwargs.get('server_pk')
        if not server_pk:
            self.logger.error('No server_pk provided to job.')
            return

        from .models import DHCPServer
        try:
            server = DHCPServer.objects.get(pk=server_pk)
        except DHCPServer.DoesNotExist:
            self.logger.error(f'DHCPServer pk={server_pk} not found.')
            return

        from .import_logic import run_import
        self.logger.info(f'Starting import from {server.name} ({server.hostname})')
        results = run_import(server)

        for category, data in results.items():
            created = data.get('created', [])
            skipped = data.get('skipped', [])
            errors = data.get('errors', [])
            self.logger.info(
                f'{category}: {len(created)} created, {len(skipped)} skipped, {len(errors)} error(s)'
            )
            for item in created:
                self.logger.info(f'  Created: {item}')
            for item in errors:
                self.logger.error(f'  Error: {item}')

        self.logger.info(f'Import complete for {server.name}')
