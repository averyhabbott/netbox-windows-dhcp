"""
Background sync job for netbox-windows-dhcp.

Sync logic:
  For each DHCPServer:
    1. Fetch /scopes from PSU.
    2. Match returned scopes against DHCPScope objects (by network address).
    3. If sync_ip_addresses == True:
         - Fetch leases + reservations.
         - Update/create NetBox IPAddress objects with status 'dhcp' (lease) or
           'reserved' (reservation). For 'reserved' IPs without a dhcp_client_id,
           update the client_id from a discovered lease but preserve the status and dns_name.
         - Store lease hostname in DHCPLeaseInfo (non-changelog side-table).
    4. If push_reservations == True:
         - Push NetBox "reserved" IPs with a dhcp_client_id within scope ranges to DHCP server.
    5. If push_scope_info == True:
         - Push scope config to DHCP server.
"""

import logging
import uuid
from contextlib import contextmanager
from typing import Optional

from core.choices import JobIntervalChoices
from netbox.jobs import JobRunner, system_job

logger = logging.getLogger('netbox_windows_dhcp')


@contextmanager
def _change_logging(user):
    """
    Set current_request so that NetBox's post_save signal handler creates ObjectChange records.

    NetBox's handle_changed_object (core/signals.py) returns immediately if current_request
    is None, which is always the case in background jobs because JobRunner.handle() never
    sets up the request context that middleware would normally provide.
    """
    from netbox.context import current_request

    class _FakeRequest:
        def __init__(self, user):
            self.user = user
            self.id = uuid.uuid4()

    token = current_request.set(_FakeRequest(user))
    try:
        yield
    finally:
        current_request.reset(token)


def _load_settings():
    """Return the DHCPPluginSettings singleton from the database."""
    from .models import DHCPPluginSettings
    return DHCPPluginSettings.load()


# ---------------------------------------------------------------------------
# Module-level sync functions — shared by DHCPSyncJob and DHCPServerSyncJob
# ---------------------------------------------------------------------------

def _upsert_lease_info(ip_obj, lease_hostname: str, active: bool, lease_expiration=None):
    """
    Update or create the DHCPLeaseInfo side-record for ip_obj.
    This write does NOT touch the IPAddress object, so no changelog entry is generated.
    """
    from .models import DHCPLeaseInfo
    try:
        DHCPLeaseInfo.objects.update_or_create(
            ip_address=ip_obj,
            defaults={
                'lease_hostname': lease_hostname or '',
                'active': active,
                'lease_expiration': lease_expiration,
            },
        )
    except Exception:
        pass  # lease info is informational — never let it abort the main sync


def _upsert_ip_address(job_logger, ip_str: str, prefix_len: int, status: str,
                       dns_name: str, client_id: str,
                       lease_hostname: str = '', lease_expiration=None,
                       protect_tag: str = '', update_client_id: bool = False,
                       lease_status: str = 'dhcp', reservation_status: str = 'reserved'):
    """
    Create or update a NetBox IPAddress for ip_str, then upsert its DHCPLeaseInfo.

    Special case — existing IP has the reservation status but this call is from a lease:
      • Do NOT change status or dns_name (the reservation takes precedence).
      • If the IP has no dhcp_client_id, store the discovered client MAC.
      • Always upsert DHCPLeaseInfo so the lease hostname/expiry are visible.

    Sync-protection: if the IP carries the configured protect_tag, all writes are
    skipped. The only exception is when update_client_id=True and status==lease_status:
    in that case dhcp_client_id is updated to match the server's lease (to track
    server replacements), but nothing else is touched.
    """
    from ipam.models import IPAddress
    try:
        qs = IPAddress.objects.filter(address__net_host=ip_str)
        if qs.exists():
            obj = qs.first()

            # Sync-protection check — must come before any other writes.
            if protect_tag and protect_tag in obj.tags.slugs():
                if update_client_id and status == lease_status and client_id:
                    stored_client_id = obj.custom_field_data.get('dhcp_client_id')
                    if client_id != stored_client_id:
                        obj.snapshot()
                        obj.custom_field_data['dhcp_client_id'] = client_id
                        obj.save()
                        job_logger.info(
                            f'Updated dhcp_client_id on protected IP {ip_str} from lease '
                            f'(client_id={client_id})'
                        )
                    else:
                        job_logger.debug(f'Protected IP {ip_str}: dhcp_client_id already up-to-date')
                else:
                    job_logger.debug(f'Protected IP {ip_str}: skipped by sync-protect tag')
                # Always record lease metadata regardless of protection status.
                _upsert_lease_info(obj, lease_hostname=lease_hostname, active=True,
                                   lease_expiration=lease_expiration)
                return

            if obj.status == reservation_status and status == lease_status:
                # Reservation takes precedence — preserve status and dns_name.
                stored_client_id = obj.custom_field_data.get('dhcp_client_id')
                if client_id and not stored_client_id:
                    obj.snapshot()
                    obj.custom_field_data['dhcp_client_id'] = client_id
                    obj.save()
                    job_logger.info(
                        f'Updated dhcp_client_id on reserved IP {ip_str} from discovered lease '
                        f'(client_id={client_id})'
                    )
                _upsert_lease_info(obj, lease_hostname=lease_hostname, active=True,
                                   lease_expiration=lease_expiration)
                return
            obj.snapshot()
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
            obj.save()
            action = 'Created' if created else 'Updated'
            reason_str = f' [{", ".join(change_reasons)}]' if change_reasons else ''
            job_logger.info(f'{action} IP {ip_str}{reason_str}')
        else:
            job_logger.debug(f'IP {ip_str} already up-to-date (status={status})')

        _upsert_lease_info(obj, lease_hostname=lease_hostname, active=True,
                           lease_expiration=lease_expiration)

    except Exception as exc:
        job_logger.warning(f'Failed to upsert IP Address {ip_str}: {exc}', exc_info=True)


def _update_ip_addresses_from_reservations(job_logger, scope, reservations: list,
                                            protect_tag: str = '', update_client_id: bool = False,
                                            lease_status: str = 'dhcp', reservation_status: str = 'reserved'):
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
            status=reservation_status,
            dns_name=name,
            client_id=client_id,
            lease_hostname=name,    # DHCP server's name for this reservation
            lease_expiration=None,  # Reservations do not expire
            protect_tag=protect_tag,
            update_client_id=update_client_id,
            lease_status=lease_status,
            reservation_status=reservation_status,
        )


def _update_ip_addresses_from_leases(job_logger, scope, leases: list,
                                      protect_tag: str = '', update_client_id: bool = False,
                                      lease_status: str = 'dhcp', reservation_status: str = 'reserved'):
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    prefix_len = scope.prefix.prefix.prefixlen
    for lease in leases:
        ip_str = lease.get('ip_address') or lease.get('IPAddress')
        client_id = lease.get('client_id') or lease.get('ClientId') or ''
        hostname = lease.get('hostname') or lease.get('HostName') or ''
        expiry_str = lease.get('lease_expiry') or lease.get('LeaseExpiry') or ''

        if not ip_str:
            continue

        lease_expiration = None
        if expiry_str:
            try:
                lease_expiration = parse_datetime(str(expiry_str))
                if lease_expiration and timezone.is_naive(lease_expiration):
                    lease_expiration = timezone.make_aware(lease_expiration)
            except Exception:
                pass

        _upsert_ip_address(
            job_logger,
            ip_str=ip_str,
            prefix_len=prefix_len,
            status=lease_status,
            dns_name=hostname,
            client_id=client_id,
            lease_hostname=hostname,
            lease_expiration=lease_expiration,
            protect_tag=protect_tag,
            update_client_id=update_client_id,
            lease_status=lease_status,
            reservation_status=reservation_status,
        )


def _cleanup_stale_ips(job_logger, scope, lease_ips: set, reservation_ips: set,
                       push_reservations: bool, protect_tag: str = '',
                       lease_status: str = 'dhcp', reservation_status: str = 'reserved'):
    """
    Remove or downgrade IPs within a scope that no longer exist on the DHCP server.

    Lease status IPs:
      - No matching lease → delete.

    Reservation status IPs:
      - push_reservations=True → never touch (NetBox is source of truth).
      - No dhcp_client_id → manually created / pre-staged → never auto-delete.
      - Has dhcp_client_id but no DHCPLeaseInfo → manually created → leave alone.
      - Has dhcp_client_id AND DHCPLeaseInfo (DHCP-managed):
          no reservation but lease exists → downgrade to lease_status
          neither reservation nor lease → delete

    Sync-protected IPs (carrying protect_tag) are always skipped.
    """
    from ipam.models import IPAddress
    from .models import DHCPLeaseInfo

    prefix_cidr = str(scope.prefix.prefix)

    # Collect IDs of DHCP-managed IPs (those the sync has previously written)
    dhcp_managed_ids = set(
        DHCPLeaseInfo.objects.filter(
            ip_address__address__net_contained_or_equal=prefix_cidr
        ).values_list('ip_address_id', flat=True)
    )

    managed = IPAddress.objects.filter(
        address__net_contained_or_equal=prefix_cidr,
        status__in=(lease_status, reservation_status),
    ).prefetch_related('tags')

    for ip_obj in managed:
        ip_str = str(ip_obj.address.ip)

        # Skip sync-protected IPs entirely — never delete or downgrade them.
        if protect_tag and protect_tag in ip_obj.tags.slugs():
            job_logger.debug(f'Protected IP {ip_str}: skipped cleanup by sync-protect tag')
            continue

        if ip_obj.status == reservation_status:
            if push_reservations:
                # NetBox is source of truth; never remove reservations based on server state
                continue
            client_id = ip_obj.custom_field_data.get('dhcp_client_id') or ''
            if not client_id:
                # No client_id → pre-staged or manually created → leave it alone
                continue
            if ip_obj.pk not in dhcp_managed_ids:
                # Has a client_id but no DHCPLeaseInfo → manually created → leave it alone
                continue
            # DHCP-managed reservation: clean up if the server no longer has it
            if ip_str not in reservation_ips:
                if ip_str in lease_ips:
                    ip_obj.snapshot()
                    ip_obj.status = lease_status
                    ip_obj.save()
                    job_logger.info(
                        f'Downgraded IP {ip_str} {reservation_status}→{lease_status} '
                        f'(reservation removed, lease still active)'
                    )
                else:
                    job_logger.info(f'Deleting IP {ip_str} — reservation and lease no longer exist on server')
                    ip_obj.delete()

        elif ip_obj.status == lease_status and ip_str not in lease_ips:
            job_logger.info(f'Deleting IP {ip_str} — lease expired or no longer exists on server')
            ip_obj.delete()


def _push_reservations(job_logger, client, scope, scope_id: str, reservation_status: str = 'reserved'):
    from ipam.models import IPAddress

    existing_reservations = {
        r.get('ip_address') or r.get('IPAddress'): r
        for r in client.list_reservations(scope_id=scope_id)
    }

    prefix_cidr = str(scope.prefix.prefix)
    seen_client_ids = set()
    for ip_obj in IPAddress.objects.filter(
        status=reservation_status,
        address__net_contained_or_equal=prefix_cidr,
    ):
        try:
            host = str(ip_obj.address.ip)
            client_id = ip_obj.custom_field_data.get('dhcp_client_id') or ''
            if not client_id:
                job_logger.debug(
                    f'Skipping reservation {host} — no dhcp_client_id set '
                    f'(Windows DHCP requires a client MAC to create a reservation)'
                )
                continue
            if client_id in seen_client_ids:
                job_logger.warning(
                    f'Skipping reservation {host} — client_id {client_id} already pushed '
                    f'for another IP in scope {scope_id} (duplicate MACs not allowed per scope)'
                )
                continue
            seen_client_ids.add(client_id)
            if host not in existing_reservations:
                client.create_reservation({
                    'scope_id': scope_id,
                    'ip_address': host,
                    'client_id': client_id,
                    'name': ip_obj.dns_name or '',
                    'description': ip_obj.description or '',
                    'type': 'Dhcp',
                })
                job_logger.info(f'Pushed reservation {host} to server (client_id={client_id})')
        except Exception as exc:
            job_logger.warning(f'Failed to push reservation {ip_obj}: {exc}')


def _pull_exclusions(job_logger, client, scope, scope_id: str):
    """
    Reconcile NetBox exclusion ranges to match the live DHCP server (server is authoritative).
    Called when push_scope_info=False.
    - Server exclusions missing from NetBox are created.
    - NetBox exclusions no longer on the server are deleted.
    """
    from .api_client import PSUClientError
    from .models import DHCPExclusionRange

    try:
        remote_raw = client.list_exclusions(scope_id)
    except PSUClientError as exc:
        job_logger.warning(f'Scope {scope_id}: could not fetch exclusion ranges — skipping reconciliation: {exc}')
        return

    remote = {
        (r.get('start_ip') or r.get('StartRange'), r.get('end_ip') or r.get('EndRange'))
        for r in remote_raw
        if (r.get('start_ip') or r.get('StartRange')) and (r.get('end_ip') or r.get('EndRange'))
    }
    local_qs = scope.exclusion_ranges.all()
    local = {(ex.start_ip, ex.end_ip): ex for ex in local_qs}

    for start, end in remote - set(local.keys()):
        DHCPExclusionRange.objects.create(scope=scope, start_ip=start, end_ip=end)
        job_logger.info(f'Scope {scope_id}: added exclusion {start}–{end} from server')

    for (start, end), ex in local.items():
        if (start, end) not in remote:
            job_logger.info(f'Scope {scope_id}: removed exclusion {start}–{end} — no longer on server')
            ex.delete()


def _sync_exclusions(job_logger, client, scope, scope_id: str):
    """
    Reconcile exclusion ranges between NetBox and the DHCP server.

    Called only when push_scope_info=True (NetBox is source of truth).
    - Exclusions in NetBox but missing from server → created on server.
    - Exclusions on server but absent from NetBox → removed from server.
    """
    from .api_client import PSUClientError

    try:
        remote_raw = client.list_exclusions(scope_id)
    except PSUClientError as exc:
        job_logger.warning(f'Scope {scope_id}: could not fetch remote exclusions — skipping reconciliation: {exc}')
        return

    remote = {
        (r.get('start_ip') or r.get('StartRange'), r.get('end_ip') or r.get('EndRange'))
        for r in remote_raw
        if (r.get('start_ip') or r.get('StartRange')) and (r.get('end_ip') or r.get('EndRange'))
    }
    local = {
        (ex.start_ip, ex.end_ip)
        for ex in scope.exclusion_ranges.all()
    }

    for start, end in local - remote:
        try:
            client.create_exclusion({'scope_id': scope_id, 'start_ip': start, 'end_ip': end})
            job_logger.info(f'Scope {scope_id}: pushed exclusion {start}–{end} to server')
        except PSUClientError as exc:
            job_logger.warning(f'Scope {scope_id}: failed to push exclusion {start}–{end}: {exc}')

    for start, end in remote - local:
        try:
            client.delete_exclusion({'scope_id': scope_id, 'start_ip': start, 'end_ip': end})
            job_logger.info(f'Scope {scope_id}: removed exclusion {start}–{end} from server (not in NetBox)')
        except PSUClientError as exc:
            job_logger.warning(f'Scope {scope_id}: failed to remove exclusion {start}–{end}: {exc}')


def _pull_scope_attributes(job_logger, scope, remote):
    """
    Update NetBox scope fields to match the live DHCP server (server is authoritative).
    Called on every sync when push_scope_info=False.
    Logs and saves only fields that actually differ.
    """
    scope_label = scope.name  # capture before any name change

    remote_name   = remote.get('name')     or remote.get('Name')       or ''
    remote_start  = remote.get('start_ip') or remote.get('StartRange') or ''
    remote_end    = remote.get('end_ip')   or remote.get('EndRange')   or ''
    router_raw    = remote.get('router')   or remote.get('Router')     or ''
    remote_router = router_raw if router_raw not in ('', '0.0.0.0') else None
    remote_lease  = int(remote.get('lease_duration_seconds') or remote.get('LeaseDuration') or 86400)

    # Collect (field_name, old_value, new_value) tuples for fields that need updating.
    changes = []
    if remote_name and scope.name != remote_name:
        changes.append(('name', scope.name, remote_name))
    if remote_start and scope.start_ip != remote_start:
        changes.append(('start_ip', scope.start_ip, remote_start))
    if remote_end and scope.end_ip != remote_end:
        changes.append(('end_ip', scope.end_ip, remote_end))
    if scope.router != remote_router:
        changes.append(('router', scope.router, remote_router))
    if scope.lease_lifetime != remote_lease:
        changes.append(('lease_lifetime', scope.lease_lifetime, remote_lease))

    if not changes:
        job_logger.debug(f'Scope "{scope_label}": all attributes match server')
        return

    scope.snapshot()
    for field, old_val, new_val in changes:
        setattr(scope, field, new_val)
        job_logger.info(f'Scope "{scope_label}": {field} updated {old_val!r} → {new_val!r} from server')
    scope.save(update_fields=[f for f, _, _ in changes])


def _push_scope(job_logger, client, scope, remote=None, scope_id: Optional[str] = None):
    """
    Push NetBox scope attributes to the DHCP server (NetBox is authoritative).
    When `remote` is provided, skips the push if all fields already match.
    When `scope_id` is None, creates the scope on the server instead of updating.
    """
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

        if not scope_id:
            client.create_scope(payload)
            job_logger.info(f'Created scope {payload["scope_id"]} on server')
            return

        # Compare with remote to avoid pushing when nothing has changed.
        if remote is not None:
            router_raw = remote.get('router') or remote.get('Router') or ''
            remote_router = router_raw if router_raw not in ('', '0.0.0.0') else None
            remote_lease = int(remote.get('lease_duration_seconds') or remote.get('LeaseDuration') or 86400)
            diffs = []
            if (remote.get('name') or remote.get('Name') or '') != scope.name:
                diffs.append('name')
            if (remote.get('start_ip') or remote.get('StartRange') or '') != scope.start_ip:
                diffs.append('start_ip')
            if (remote.get('end_ip') or remote.get('EndRange') or '') != scope.end_ip:
                diffs.append('end_ip')
            if remote_router != scope.router:
                diffs.append('router')
            if remote_lease != scope.lease_lifetime:
                diffs.append('lease_lifetime')
            if not diffs:
                job_logger.debug(f'Scope {scope_id}: already matches NetBox — no push needed')
                return
            job_logger.info(f'Scope {scope_id}: pushing changes to server — field(s) differ: {", ".join(diffs)}')

        client.update_scope(scope_id, payload)
        job_logger.info(f'Updated scope {scope_id} on server')
    except Exception as exc:
        job_logger.warning(f'Failed to push scope {scope}: {exc}')


def _sync_server(job_logger, server, sync_ip_addresses: bool, push_reservations: bool,
                 push_scope_info: bool, protect_tag: str = '', update_client_id: bool = False,
                 lease_status: str = 'dhcp', reservation_status: str = 'reserved'):
    from .api_client import PSUClient, PSUClientError
    from .models import DHCPFailover, DHCPScope

    # Pre-flight check: skip connecting to this server entirely if there is nothing
    # eligible to sync on it.
    #   - Standalone scopes: only relevant if sync_standalone_scopes is enabled.
    #   - Failover scopes: only synced via the PRIMARY server; secondary-only servers
    #     serve no role in syncing scope data.
    has_eligible_failover = DHCPFailover.objects.filter(
        primary_server=server, sync_enabled=True
    ).exists()
    if not server.sync_standalone_scopes and not has_eligible_failover:
        job_logger.info(
            f'Skipping server {server.name}: sync_standalone_scopes is disabled and '
            f'server is not primary for any active failover relationship.'
        )
        return

    job_logger.info(
        f'Syncing server: {server.name} ({server.hostname}) — '
        f'sync_ip_addresses={sync_ip_addresses} push_reservations={push_reservations} push_scope_info={push_scope_info}'
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

        # Sync eligibility check:
        #   - Scopes with a failover relationship are synced only when that failover's
        #     sync_enabled is True.
        #   - Scopes with a server FK are synced only against their owning server, and
        #     only when that server's sync_standalone_scopes is True.
        #   - Scopes with neither set are skipped silently (legacy data).
        if scope.failover:
            if not scope.failover.sync_enabled:
                job_logger.debug(
                    f'Scope "{scope.name}": failover "{scope.failover.name}" has sync disabled — skipping'
                )
                continue
        elif scope.server_id:
            if scope.server_id != server.pk:
                # Belongs to a different server — skip silently
                continue
            if not server.sync_standalone_scopes:
                job_logger.debug(
                    f'Scope "{scope.name}": standalone scopes disabled on {server.name} — skipping'
                )
                continue
        else:
            job_logger.debug(f'Scope "{scope.name}": no server or failover assigned — skipping')
            continue

        job_logger.info(f'Scope "{scope.name}" matched remote scope_id={scope_id} on {server.name}')

        if sync_ip_addresses:
            try:
                leases = client.list_leases(scope_id=scope_id)
                reservations = client.list_reservations(scope_id=scope_id)
            except PSUClientError as exc:
                job_logger.error(
                    f'Failed to fetch leases/reservations for scope {scope_id}: {exc} — skipping cleanup'
                )
                leases, reservations = None, None

            if leases is not None:
                job_logger.info(f'Scope {scope_id}: {len(leases)} lease(s), {len(reservations)} reservation(s)')
                _update_ip_addresses_from_reservations(
                    job_logger, scope, reservations,
                    protect_tag=protect_tag, update_client_id=update_client_id,
                    lease_status=lease_status, reservation_status=reservation_status,
                )
                _update_ip_addresses_from_leases(
                    job_logger, scope, leases,
                    protect_tag=protect_tag, update_client_id=update_client_id,
                    lease_status=lease_status, reservation_status=reservation_status,
                )

                lease_ips = {
                    r.get('ip_address') or r.get('IPAddress')
                    for r in leases
                    if r.get('ip_address') or r.get('IPAddress')
                }
                reservation_ips = {
                    r.get('ip_address') or r.get('IPAddress')
                    for r in reservations
                    if r.get('ip_address') or r.get('IPAddress')
                }
                _cleanup_stale_ips(
                    job_logger, scope, lease_ips, reservation_ips, push_reservations,
                    protect_tag=protect_tag,
                    lease_status=lease_status, reservation_status=reservation_status,
                )
        else:
            job_logger.info(f'Scope {scope_id}: skipping IP updates (sync_ip_addresses=False)')

        if push_reservations:
            try:
                _push_reservations(job_logger, client, scope, scope_id,
                                   reservation_status=reservation_status)
            except PSUClientError as exc:
                job_logger.error(f'Failed to push reservations for scope {scope_id}: {exc}')

        if push_scope_info:
            _push_scope(job_logger, client, scope, remote=remote, scope_id=scope_id)
            _sync_exclusions(job_logger, client, scope, scope_id)
        else:
            _pull_scope_attributes(job_logger, scope, remote)
            _pull_exclusions(job_logger, client, scope, scope_id)

    # Handle local scopes that have no matching remote scope on this server.
    # Only consider scopes linked to this server via their failover relationship.
    for network, scope in local_scope_map.items():
        if network in remote_scope_map:
            continue  # already handled in the loop above

        is_this_server = (
            (scope.server_id == server.pk) or
            (scope.failover_id and (
                scope.failover.primary_server_id == server.pk
                or scope.failover.secondary_server_id == server.pk
            ))
        )
        if not is_this_server:
            continue

        if scope.failover_id and not scope.failover.sync_enabled:
            continue

        if push_scope_info:
            # Push scope info is on — create the missing scope on the server.
            _push_scope(job_logger, client, scope)
        else:
            # Server is reachable and doesn't know this scope — remove it from NetBox.
            job_logger.info(
                f'Deleting scope "{scope.name}" ({network}) — '
                f'no longer exists on {server.name} and push_scope_info is disabled'
            )
            try:
                scope.delete()
            except Exception as exc:
                job_logger.warning(f'Failed to delete scope "{scope.name}" ({network}): {exc}')


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
        from datetime import timedelta

        from django.utils import timezone

        start_time = timezone.now()

        try:
            from .models import DHCPServer
            servers = DHCPServer.objects.all()
            if not servers.exists():
                self.logger.info('No DHCP servers configured — nothing to sync.')
                return

            cfg = _load_settings()
            sync_ip_addresses = cfg.sync_ip_addresses
            push_reservations = cfg.push_reservations
            push_scope_info = cfg.push_scope_info
            protect_tag = cfg.sync_protect_tag.slug if cfg.sync_protect_tag_id else ''
            update_client_id = cfg.sync_protect_update_client_id
            lease_status = cfg.lease_status
            reservation_status = cfg.reservation_status

            self.logger.info(
                f'Starting DHCP sync: sync_ip_addresses={sync_ip_addresses} '
                f'push_reservations={push_reservations} push_scope_info={push_scope_info} '
                f'protect_tag={protect_tag!r} update_client_id={update_client_id} '
                f'lease_status={lease_status!r} reservation_status={reservation_status!r}'
            )

            with _change_logging(self.job.user):
                for server in servers:
                    try:
                        _sync_server(
                            self.logger, server, sync_ip_addresses, push_reservations, push_scope_info,
                            protect_tag=protect_tag, update_client_id=update_client_id,
                            lease_status=lease_status, reservation_status=reservation_status,
                        )
                    except Exception as exc:
                        self.logger.error(f'Error syncing server {server.name}: {exc}')

        finally:
            # Re-read settings so the interval reflects any changes made since this run started.
            cfg = _load_settings()
            interval = cfg.sync_interval

            # Suppress NetBox's default auto-reschedule (which anchors to completion time).
            # We manually enqueue the next run anchored to start_time to prevent drift.
            self.job.interval = None
            self.job.save(update_fields=['interval'])

            DHCPSyncJob.enqueue(
                user=self.job.user,
                schedule_at=start_time + timedelta(minutes=interval),
                interval=interval,
                queue_name=cfg.sync_queue,
            )


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
        with _change_logging(self.job.user):
            _sync_server(
                self.logger, server,
                cfg.sync_ip_addresses, cfg.push_reservations, cfg.push_scope_info,
                protect_tag=cfg.sync_protect_tag.slug if cfg.sync_protect_tag_id else '',
                update_client_id=cfg.sync_protect_update_client_id,
                lease_status=cfg.lease_status,
                reservation_status=cfg.reservation_status,
            )


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
        with _change_logging(self.job.user):
            results = run_import(server)

        category_labels = {
            'failovers':        'Failovers',
            'scopes':           'Scopes',
            'option_values':    'Option Values',
            'exclusion_ranges': 'Exclusion Ranges',
        }
        for category, data in results.items():
            created = data.get('created', [])
            skipped = data.get('skipped', [])
            errors = data.get('errors', [])
            label = category_labels.get(category, category)
            self.logger.info(
                f'{label}: {len(created)} created, {len(skipped)} skipped, {len(errors)} error(s)'
            )
            for item in created:
                self.logger.info(f'  Created: {item}')
            for item in errors:
                self.logger.error(f'  Error: {item}')

        self.logger.info(f'Import complete for {server.name}')
