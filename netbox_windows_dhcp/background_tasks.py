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

def _upsert_ip_address(job_logger, ip_str: str, status: str, dns_name: str, description: str):
    from ipam.models import IPAddress
    try:
        obj, created = IPAddress.objects.get_or_create(
            address=f'{ip_str}/32',
            defaults={
                'status': status,
                'dns_name': dns_name[:255] if dns_name else '',
                'description': description[:200] if description else '',
            },
        )
        if not created:
            changed = False
            if obj.status != status:
                obj.status = status
                changed = True
            if dns_name and obj.dns_name != dns_name[:255]:
                obj.dns_name = dns_name[:255]
                changed = True
            if description and obj.description != description[:200]:
                obj.description = description[:200]
                changed = True
            if changed:
                obj.save()
        action = 'Created' if created else 'Updated'
        job_logger.debug('%s IP Address %s → status=%s', action, ip_str, status)
    except Exception as exc:
        job_logger.warning('Failed to upsert IP Address %s: %s', ip_str, exc)


def _update_ip_addresses_from_reservations(job_logger, scope, reservations: list):
    for res in reservations:
        ip_str = res.get('ip_address') or res.get('IPAddress')
        client_id = res.get('client_id') or res.get('ClientId') or ''
        name = res.get('name') or res.get('Name') or ''
        if not ip_str:
            continue
        _upsert_ip_address(
            job_logger,
            ip_str=ip_str,
            status='dhcp-reserved',
            dns_name=name,
            description=f'DHCP Reservation | Client ID: {client_id}' if client_id else 'DHCP Reservation',
        )


def _update_ip_addresses_from_leases(job_logger, scope, leases: list):
    from ipam.models import IPAddress
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
            status='dhcp-lease',
            dns_name=hostname,
            description=f'DHCP Lease | Client ID: {client_id}' if client_id else 'DHCP Lease',
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
                job_logger.info('Pushed reservation %s to server', host)
        except Exception as exc:
            job_logger.warning('Failed to push reservation %s: %s', ip_obj, exc)


def _push_scope(job_logger, client, scope, scope_id: Optional[str] = None):
    from netaddr import IPNetwork
    try:
        prefix_net = IPNetwork(str(scope.prefix.prefix))
        payload = {
            'scope_id': scope_id or str(prefix_net.network_address),
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
            job_logger.info('Updated scope %s on server', scope_id)
        else:
            client.create_scope(payload)
            job_logger.info('Created scope %s on server', payload['scope_id'])
    except Exception as exc:
        job_logger.warning('Failed to push scope %s: %s', scope, exc)


def _sync_server(job_logger, server, sync_mode: str, push_reservations: bool, push_scope_info: bool):
    from .api_client import PSUClient, PSUClientError
    from .models import DHCPScope

    job_logger.info('Syncing server: %s (%s)', server.name, server.hostname)
    client = PSUClient(server)

    try:
        remote_scopes = client.list_scopes()
    except PSUClientError as exc:
        job_logger.error('Failed to fetch scopes from %s: %s', server.name, exc)
        return

    # Build a map: network_address -> remote scope dict
    remote_scope_map = {}
    for rs in remote_scopes:
        scope_id = rs.get('scope_id') or rs.get('ScopeId') or rs.get('network_address')
        if scope_id:
            remote_scope_map[scope_id] = rs

    local_scopes = DHCPScope.objects.select_related('prefix', 'failover').prefetch_related(
        'option_values__option_definition'
    )

    for scope in local_scopes:
        try:
            prefix_network = str(scope.prefix.prefix.network_address)
        except Exception:
            continue

        remote = remote_scope_map.get(prefix_network)
        if remote is None:
            job_logger.debug(
                'No remote scope found for prefix %s on server %s',
                scope.prefix, server.name,
            )
            if push_scope_info:
                _push_scope(job_logger, client, scope)
            continue

        scope_id = remote.get('scope_id') or remote.get('ScopeId') or prefix_network

        if sync_mode == 'active':
            try:
                leases = client.list_leases(scope_id=scope_id)
                reservations = client.list_reservations(scope_id=scope_id)
            except PSUClientError as exc:
                job_logger.error(
                    'Failed to fetch leases/reservations for scope %s: %s', scope_id, exc
                )
                leases, reservations = [], []

            _update_ip_addresses_from_reservations(job_logger, scope, reservations)
            _update_ip_addresses_from_leases(job_logger, scope, leases)

        if push_reservations:
            try:
                _push_reservations(job_logger, client, scope, scope_id)
            except PSUClientError as exc:
                job_logger.error('Failed to push reservations for scope %s: %s', scope_id, exc)

        if push_scope_info:
            _push_scope(job_logger, client, scope, scope_id)


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
            'Starting DHCP sync: mode=%s push_reservations=%s push_scope_info=%s',
            sync_mode, push_reservations, push_scope_info,
        )

        for server in servers:
            try:
                _sync_server(self.logger, server, sync_mode, push_reservations, push_scope_info)
            except Exception as exc:
                self.logger.error('Error syncing server %s: %s', server.name, exc)


class DHCPServerSyncJob(JobRunner):
    """Sync a single DHCPServer on demand (enqueued by the Sync Now button)."""

    class Meta:
        name = 'Windows DHCP Server Sync'
        description = 'On-demand sync for a single Windows DHCP server.'

    def run(self, *args, **kwargs):
        server = self.job.object
        if not server:
            self.logger.error('No server attached to job.')
            return

        cfg = _load_settings()
        _sync_server(self.logger, server, cfg.scope_sync_mode, cfg.push_reservations, cfg.push_scope_info)
