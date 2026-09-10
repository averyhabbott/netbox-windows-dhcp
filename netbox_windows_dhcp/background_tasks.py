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

from netbox.jobs import JobRunner, system_job

logger = logging.getLogger('netbox_windows_dhcp')


class _WarnOnlyPropagator(logging.Handler):
    """
    Forwards only WARNING+ records from a per-job logger to the root logger.
    Used to keep verbose per-scope INFO/DEBUG out of netbox.log while preserving
    the in-UI job log (captured by NetBox's JobLogHandler at the source logger).
    """

    def emit(self, record):
        if record.levelno >= logging.WARNING:
            logging.getLogger().handle(record)


def _quiet_file_log(job_logger):
    """Disable propagation and add the WARNING+ forwarder, idempotently."""
    job_logger.propagate = False
    if not any(isinstance(h, _WarnOnlyPropagator) for h in job_logger.handlers):
        job_logger.addHandler(_WarnOnlyPropagator())


@contextmanager
def _change_logging():
    """
    Set current_request so that NetBox's post_save signal handler creates ObjectChange records
    attributed to the DHCP-Sync-Service account.

    Always uses the service account regardless of who triggered the job, so that sync-driven
    changes are clearly distinguished from human edits in the audit trail. NetBox's
    handle_changed_object (core/signals.py) returns immediately if current_request is None,
    which is always the case in background jobs — this context manager provides the fake
    request that enables change logging.
    """
    from django.contrib.auth import get_user_model
    from netbox.context import current_request

    User = get_user_model()
    try:
        service_user = User.objects.get(username='DHCP-Sync-Service')
    except User.DoesNotExist:
        # Service account not yet created (e.g. before first migrate). Skip rather than crash.
        yield
        return

    class _FakeRequest:
        def __init__(self, user):
            self.user = user
            self.id = uuid.uuid4()

    token = current_request.set(_FakeRequest(service_user))
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
                       lease_status: str = 'dhcp', reservation_status: str = 'reserved',
                       protected_prefix_networks=frozenset()):
    """
    Create or update a NetBox IPAddress for ip_str, then upsert its DHCPLeaseInfo.

    Special case — existing IP has the reservation status but this call is from a lease:
      • Do NOT change status or dns_name (the reservation takes precedence).
      • If the IP has no dhcp_client_id, store the discovered client MAC.
      • Always upsert DHCPLeaseInfo so the lease hostname/expiry are visible.

    Sync-protection: if the IP carries the configured protect_tag or falls within a
    tagged prefix (protected_prefix_networks), all writes are skipped. The only
    exception is when update_client_id=True and status==lease_status and the IP
    has the protect_tag directly: in that case dhcp_client_id is updated (to track
    server replacements) but nothing else is touched.
    """
    import ipaddress as _ipmod

    from ipam.models import IPAddress

    # Prefix containment check — independent of DB lookup.
    prefix_protected = False
    if protected_prefix_networks:
        try:
            _addr = _ipmod.ip_address(ip_str)
            prefix_protected = any(_addr in net for net in protected_prefix_networks)
        except ValueError:
            pass

    try:
        qs = IPAddress.objects.filter(address__net_host=ip_str)
        if qs.exists():
            obj = qs.first()

            tag_protected = bool(protect_tag and protect_tag in obj.tags.slugs())

            # Sync-protection check — must come before any other writes.
            if tag_protected or prefix_protected:
                if tag_protected and update_client_id and status == lease_status and client_id:
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
                    source = 'sync-protect tag' if tag_protected else 'protected prefix'
                    job_logger.debug(f'Protected IP {ip_str}: skipped by {source}')
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
            if prefix_protected:
                job_logger.debug(f'Protected IP {ip_str}: creation skipped (in protected prefix)')
                return
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
                                            lease_status: str = 'dhcp', reservation_status: str = 'reserved',
                                            protected_prefix_networks=frozenset()):
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
            protected_prefix_networks=protected_prefix_networks,
        )


def _update_ip_addresses_from_leases(job_logger, scope, leases: list,
                                      protect_tag: str = '', update_client_id: bool = False,
                                      lease_status: str = 'dhcp', reservation_status: str = 'reserved',
                                      protected_prefix_networks=frozenset()):
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
            protected_prefix_networks=protected_prefix_networks,
        )


def _cleanup_stale_ips(job_logger, scope, lease_ips: set, reservation_ips: set,
                       push_reservations: bool, protect_tag: str = '',
                       lease_status: str = 'dhcp', reservation_status: str = 'reserved',
                       protected_prefix_networks=frozenset()):
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

    import ipaddress as _ipmod

    for ip_obj in managed:
        ip_str = str(ip_obj.address.ip)

        # Skip sync-protected IPs entirely — never delete or downgrade them.
        if protect_tag and protect_tag in ip_obj.tags.slugs():
            job_logger.debug(f'Protected IP {ip_str}: skipped cleanup by sync-protect tag')
            continue

        if protected_prefix_networks:
            try:
                _addr = _ipmod.ip_address(ip_str)
                if any(_addr in net for net in protected_prefix_networks):
                    job_logger.debug(f'Protected IP {ip_str}: skipped cleanup (in protected prefix)')
                    continue
            except ValueError:
                pass

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


def _sync_exclusions(job_logger, client, scope, scope_id: str) -> bool:
    """
    Reconcile exclusion ranges between NetBox and the DHCP server.

    Called only when push_scope_info=True (NetBox is source of truth).
    - Exclusions in NetBox but missing from server → created on server.
    - Exclusions on server but absent from NetBox → removed from server.

    Returns True if at least one exclusion was actually created or removed on
    the server (used by the caller to decide whether this scope needs failover
    replication).
    """
    from .api_client import PSUClientError

    try:
        remote_raw = client.list_exclusions(scope_id)
    except PSUClientError as exc:
        job_logger.warning(f'Scope {scope_id}: could not fetch remote exclusions — skipping reconciliation: {exc}')
        return False

    remote = {
        (r.get('start_ip') or r.get('StartRange'), r.get('end_ip') or r.get('EndRange'))
        for r in remote_raw
        if (r.get('start_ip') or r.get('StartRange')) and (r.get('end_ip') or r.get('EndRange'))
    }
    local = {
        (ex.start_ip, ex.end_ip)
        for ex in scope.exclusion_ranges.all()
    }

    changed = False

    for start, end in local - remote:
        try:
            client.create_exclusion({'scope_id': scope_id, 'start_ip': start, 'end_ip': end})
            job_logger.info(f'Scope {scope_id}: pushed exclusion {start}–{end} to server')
            changed = True
        except PSUClientError as exc:
            job_logger.warning(f'Scope {scope_id}: failed to push exclusion {start}–{end}: {exc}')

    for start, end in remote - local:
        try:
            client.delete_exclusion({'scope_id': scope_id, 'start_ip': start, 'end_ip': end})
            job_logger.info(f'Scope {scope_id}: removed exclusion {start}–{end} from server (not in NetBox)')
            changed = True
        except PSUClientError as exc:
            job_logger.warning(f'Scope {scope_id}: failed to remove exclusion {start}–{end}: {exc}')

    return changed


def _local_option_map(scope):
    """{code: DHCPOptionValue} for a scope's option values, excluding codes 3/51
    (Router/Lease Time — handled as scope fields, see _pull_scope_attributes)."""
    from .import_logic import SCOPE_FIELD_OPTION_CODES
    return {
        ov.option_definition.code: ov
        for ov in scope.option_values.select_related('option_definition').all()
        if ov.option_definition.code not in SCOPE_FIELD_OPTION_CODES
    }


def _remote_option_map(remote_raw):
    """{code: value} parsed from a PSU list_scope_options() response, excluding
    codes 3/51 (Router/Lease Time — handled as scope fields)."""
    from .import_logic import SCOPE_FIELD_OPTION_CODES, normalize_option_value
    remote = {}
    for ro in remote_raw:
        parsed = normalize_option_value(ro)
        if parsed is None:
            continue
        code, value, _name, _vendor_class = parsed
        if code in SCOPE_FIELD_OPTION_CODES:
            continue
        remote[code] = value
    return remote


def _pull_options(job_logger, client, scope, scope_id: str):
    """
    Reconcile NetBox option values to match the live DHCP server (server is authoritative).
    Called on every sync when push_scope_info=False.
    - Server options missing or changed in NetBox are added/replaced.
    - NetBox options no longer on the server are unlinked from the scope — the
      shared DHCPOptionValue row itself is never deleted, since other scopes may
      reference the same value (see DHCPOptionValue's docstring).
    Options 3 (Router) and 51 (Lease Time) are excluded — handled by _pull_scope_attributes.
    """
    from .api_client import PSUClientError
    from .import_logic import get_or_create_option_value

    try:
        remote_raw = client.list_scope_options(scope_id)
    except PSUClientError as exc:
        job_logger.warning(f'Scope {scope_id}: could not fetch remote options — skipping reconciliation: {exc}')
        return

    remote = _remote_option_map(remote_raw)
    local = _local_option_map(scope)

    for code, value in remote.items():
        existing = local.get(code)
        if existing is not None and existing.value == value:
            continue
        opt_val, _created = get_or_create_option_value(code, value)
        scope.option_values.add(opt_val)
        if existing is not None:
            scope.option_values.remove(existing)
            job_logger.info(f'Scope "{scope.name}": option {code} changed {existing.value!r} → {value!r} from server')
        else:
            job_logger.info(f'Scope "{scope.name}": option {code} added {value!r} from server')

    for code, ov in local.items():
        if code not in remote:
            scope.option_values.remove(ov)
            job_logger.info(f'Scope "{scope.name}": option {code} removed — no longer on server')


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


def _pull_scope_failover(job_logger, scope, remote, server):
    """
    Update NetBox's failover assignment to match the live DHCP server (server is
    authoritative). Called on every sync when push_scope_info=False, alongside
    _pull_scope_attributes. `server` is the DHCPServer being synced, used to
    populate `scope.server` if the scope becomes standalone (server/failover are
    mutually exclusive — see DHCPScope.clean()).

    If the server reports a relationship name with no matching DHCPFailover in
    NetBox, this only logs an error — the fix is re-running "Import from Server"
    for this DHCP server (which creates any missing DHCPFailover records), after
    which this resolves itself on the next scheduled sync.
    """
    from .models import DHCPFailover

    remote_failover_name = (
        remote.get('failover_name') or remote.get('FailoverName')
        or remote.get('FailoverRelationshipName') or None
    )
    current_name = scope.failover.name if scope.failover_id else None

    if current_name == remote_failover_name:
        return

    if not remote_failover_name:
        scope.snapshot()
        scope.failover = None
        scope.server = server
        scope.save(update_fields=['failover', 'server'])
        job_logger.info(f'Scope "{scope.name}": failover cleared (was {current_name!r}) — server reports standalone')
        return

    failover = DHCPFailover.objects.filter(name=remote_failover_name).first()
    if failover is None:
        job_logger.error(
            f'Scope "{scope.name}": server reports failover relationship {remote_failover_name!r}, '
            f'which does not exist in NetBox — re-run "Import from Server" for this DHCP server to '
            f'create it, then this will resolve on the next sync.'
        )
        return

    scope.snapshot()
    scope.failover = failover
    scope.server = None
    scope.save(update_fields=['failover', 'server'])
    job_logger.info(f'Scope "{scope.name}": failover updated {current_name!r} → {remote_failover_name!r} from server')


def _push_scope(job_logger, client, scope, remote=None, scope_id: Optional[str] = None):
    """
    Push NetBox scope attributes and option values to the DHCP server (NetBox is
    authoritative). When `remote` is provided, skips the push if nothing differs.
    When `scope_id` is None, creates the scope on the server instead of updating.

    Option values are folded into this same create/update payload (an `options`
    key with `set`/`remove` lists) rather than a separate endpoint/round-trip —
    the scope create/update endpoint already works as a declarative "apply
    exactly this" call (see how `router` is handled server-side), so PSU just
    needs to apply the same instructions for arbitrary option codes. Failover
    relationship membership (a `failover` key with `enroll`/`remove`) is
    folded in the same way — NetBox is source of truth for failover assignment
    when push_scope_info=True.

    Returns True if a create/update was actually sent to the server (used by
    the caller to decide whether this scope needs failover replication),
    False if nothing needed pushing or the push failed.
    """
    from netaddr import IPNetwork

    from .api_client import PSUClientError
    from .import_logic import denormalize_option_value

    local_failover_name = scope.failover.name if scope.failover_id else None
    failover_attempted = False

    try:
        prefix_net = IPNetwork(str(scope.prefix.prefix))
        local_options = {code: ov.value for code, ov in _local_option_map(scope).items()}
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
            if local_options:
                payload['options'] = {
                    'set': [{'code': code, 'value': denormalize_option_value(value)}
                            for code, value in local_options.items()],
                    'remove': [],
                }
            if local_failover_name:
                payload['failover'] = {'enroll': local_failover_name}
                failover_attempted = True
            client.create_scope(payload)
            job_logger.info(f'Created scope {payload["scope_id"]} on server')
            return True

        # Compare with remote to avoid pushing when nothing has changed.
        if remote is not None:
            router_raw = remote.get('router') or remote.get('Router') or ''
            remote_router = router_raw if router_raw not in ('', '0.0.0.0') else None
            remote_lease = int(remote.get('lease_duration_seconds') or remote.get('LeaseDuration') or 86400)
            remote_failover_name = (
                remote.get('failover_name') or remote.get('FailoverName')
                or remote.get('FailoverRelationshipName') or None
            )
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

            options_set = []
            options_remove = []
            try:
                remote_options = _remote_option_map(client.list_scope_options(scope_id))
            except PSUClientError as exc:
                job_logger.warning(f'Scope {scope_id}: could not fetch remote options — skipping option push: {exc}')
                remote_options = None
            if remote_options is not None:
                for code, value in local_options.items():
                    if remote_options.get(code) != value:
                        options_set.append({'code': code, 'value': denormalize_option_value(value)})
                options_remove = [code for code in remote_options if code not in local_options]
                if options_set or options_remove:
                    diffs.append('options')

            failover_payload = {}
            if local_failover_name != remote_failover_name:
                if remote_failover_name:
                    failover_payload['remove'] = remote_failover_name
                if local_failover_name:
                    failover_payload['enroll'] = local_failover_name
                diffs.append('failover')

            if not diffs:
                job_logger.debug(f'Scope {scope_id}: already matches NetBox — no push needed')
                return False
            job_logger.info(f'Scope {scope_id}: pushing changes to server — field(s) differ: {", ".join(diffs)}')
            if options_set or options_remove:
                payload['options'] = {'set': options_set, 'remove': options_remove}
                if options_set:
                    job_logger.info(f'Scope {scope_id}: pushing option(s) {[o["code"] for o in options_set]} to server')
                if options_remove:
                    job_logger.info(f'Scope {scope_id}: removing option(s) {options_remove} from server')
            if failover_payload:
                payload['failover'] = failover_payload
                failover_attempted = True
                job_logger.info(
                    f'Scope {scope_id}: failover membership {remote_failover_name!r} → {local_failover_name!r}'
                )

        client.update_scope(scope_id, payload)
        job_logger.info(f'Updated scope {scope_id} on server')
        return True
    except Exception as exc:
        job_logger.warning(f'Failed to push scope {scope}: {exc}')
        if failover_attempted:
            job_logger.error(
                f'Scope {scope}: push failed while a failover relationship change was pending '
                f'(target: {local_failover_name!r}) — failover membership is still out of sync with NetBox.'
            )
        return False


def _sync_server(job_logger, server, sync_ip_addresses: bool, push_reservations: bool,
                 push_scope_info: bool, sync_active_scopes_only: bool = False,
                 protect_tag: str = '', update_client_id: bool = False,
                 lease_status: str = 'dhcp', reservation_status: str = 'reserved',
                 fallback_failover_ids=None, protected_prefix_networks=frozenset()):
    """
    fallback_failover_ids: set of DHCPFailover PKs whose primary is down — this server
    (the secondary) should handle those failover scopes in place of the primary.
    """
    from django.utils import timezone

    from .api_client import PSUClient, PSUClientError
    from .models import DHCPFailover, DHCPScope, DHCPServer

    if server.maintenance_mode:
        job_logger.info(f'Skipping server {server.name}: maintenance mode enabled')
        return

    fallback_failover_ids = fallback_failover_ids or set()

    # Pre-flight check: skip connecting to this server entirely if there is nothing
    # eligible to sync on it.
    #   - Standalone scopes: only relevant if sync_standalone_scopes is enabled.
    #   - Failover scopes: synced via the primary server; secondary servers only handle
    #     their assigned fallback failovers when the primary is unreachable.
    has_eligible_failover = (
        DHCPFailover.objects.filter(primary_server=server, sync_enabled=True).exists()
        or bool(fallback_failover_ids)
    )
    if not server.sync_standalone_scopes and not has_eligible_failover:
        job_logger.info(
            f'Skipping server {server.name}: sync_standalone_scopes is disabled and '
            f'server has no eligible failover relationships to sync.'
        )
        return

    job_logger.info(
        f'Syncing server: {server.name} ({server.hostname}) — '
        f'sync_ip_addresses={sync_ip_addresses} push_reservations={push_reservations} push_scope_info={push_scope_info}'
    )
    client = PSUClient(server)

    try:
        remote_scopes = client.list_scopes(active_only=sync_active_scopes_only)
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

    # Scope IDs (this server's) that changed during this run and belong to a
    # failover relationship — replicated in one batched call at the end, rather
    # than per-scope, since Invoke-DhcpServerv4FailoverReplication is expensive.
    failover_replicate_scope_ids = set()

    # Iterate remote scopes only — these are the scopes that belong to this server.
    # Looking up from the remote side means we never touch scopes from other servers.
    for scope_id, remote in remote_scope_map.items():
        scope = local_scope_map.get(scope_id)
        if scope is None:
            if push_scope_info:
                # NetBox is source of truth — a remote scope NetBox doesn't know
                # about was deleted in NetBox but never removed from the server
                # (pre-existing orphan, or a failed immediate delete) — clean it
                # up. Failover-managed orphans are only cleaned up from the
                # primary's sync, mirroring _push_scopes never targeting the
                # secondary directly — deconfiguring already removes the scope
                # from the secondary as a side effect (see
                # _deconfigure_and_delete_scope).
                remote_failover_name = (
                    remote.get('failover_name') or remote.get('FailoverName')
                    or remote.get('FailoverRelationshipName') or None
                )
                if remote_failover_name:
                    failover = DHCPFailover.objects.filter(name=remote_failover_name).first()
                    if failover is None:
                        job_logger.warning(
                            f'Remote scope {scope_id} has no matching DHCPScope and reports '
                            f'failover {remote_failover_name!r}, which does not exist in '
                            f'NetBox — skipping cleanup.'
                        )
                        continue
                    if failover.primary_server_id != server.pk:
                        job_logger.debug(
                            f'Remote scope {scope_id} has no matching DHCPScope and belongs to '
                            f'failover {remote_failover_name!r} — only the primary '
                            f'({failover.primary_server.name}) cleans it up.'
                        )
                        continue
                    if failover.maintenance_mode:
                        job_logger.info(
                            f'Remote scope {scope_id}: failover {remote_failover_name!r} is in '
                            f'maintenance mode — skipping cleanup'
                        )
                        continue
                    if not failover.sync_enabled:
                        job_logger.debug(
                            f'Remote scope {scope_id}: failover {remote_failover_name!r} has sync '
                            f'disabled — skipping cleanup'
                        )
                        continue
                elif not server.sync_standalone_scopes:
                    job_logger.debug(
                        f'Remote scope {scope_id}: standalone scopes disabled on {server.name} — '
                        f'skipping cleanup'
                    )
                    continue
                job_logger.info(
                    f'Remote scope {scope_id} has no matching DHCPScope in NetBox — '
                    f'removing from {server.name}'
                )
                _deconfigure_and_delete_scope(job_logger, client, scope_id, failover_name=remote_failover_name)
                continue
            # PSU is source of truth — auto-create the scope in NetBox so it
            # participates in normal sync from this point on.
            from .import_logic import _import_scope
            _import_results = {
                'scopes':           {'created': [], 'skipped': [], 'errors': []},
                'option_values':    {'created': [], 'skipped': [], 'errors': []},
                'exclusion_ranges': {'created': [], 'skipped': [], 'errors': []},
            }
            scope = _import_scope(client, remote, _import_results, server=server)
            for created in _import_results['scopes']['created']:
                job_logger.info(f'Auto-created scope from {server.name}: {created}')
            for err in _import_results['scopes']['errors']:
                job_logger.error(f'Auto-create failed for scope on {server.name}: {err}')
            if scope is None:
                continue
            local_scope_map[scope_id] = scope

        # Scope maintenance mode check (before eligibility, so the message is clear).
        if scope.maintenance_mode:
            job_logger.info(f'Scope "{scope.name}": in maintenance mode — skipping')
            continue

        # Sync eligibility check:
        #   - Failover scopes: handled by the primary server, or by the secondary when
        #     that failover is in fallback_failover_ids (primary is down).
        #   - Standalone scopes: only relevant if sync_standalone_scopes is enabled.
        #   - Scopes with neither set are skipped silently (legacy data).
        if scope.failover:
            if scope.failover.maintenance_mode:
                job_logger.info(
                    f'Scope "{scope.name}": failover "{scope.failover.name}" is in maintenance mode — skipping'
                )
                continue
            if not scope.failover.sync_enabled:
                job_logger.debug(
                    f'Scope "{scope.name}": failover "{scope.failover.name}" has sync disabled — skipping'
                )
                continue
            is_fallback = scope.failover_id in fallback_failover_ids
            is_normal_primary = scope.failover.primary_server_id == server.pk
            if not is_fallback and not is_normal_primary:
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
                    protected_prefix_networks=protected_prefix_networks,
                )
                _update_ip_addresses_from_leases(
                    job_logger, scope, leases,
                    protect_tag=protect_tag, update_client_id=update_client_id,
                    lease_status=lease_status, reservation_status=reservation_status,
                    protected_prefix_networks=protected_prefix_networks,
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
                    protected_prefix_networks=protected_prefix_networks,
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
            scope_pushed = _push_scope(job_logger, client, scope, remote=remote, scope_id=scope_id)
            exclusions_changed = _sync_exclusions(job_logger, client, scope, scope_id)
            if scope.failover_id and (scope_pushed or exclusions_changed):
                failover_replicate_scope_ids.add(scope_id)
        else:
            _pull_scope_attributes(job_logger, scope, remote)
            _pull_scope_failover(job_logger, scope, remote, server)
            _pull_exclusions(job_logger, client, scope, scope_id)
            _pull_options(job_logger, client, scope, scope_id)

        DHCPScope.objects.filter(pk=scope.pk).update(last_sync_at=timezone.now())

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
            scope_pushed = _push_scope(job_logger, client, scope)
            if scope.failover_id and scope_pushed:
                failover_replicate_scope_ids.add(network)
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

    if failover_replicate_scope_ids:
        try:
            client.replicate_failover(scope_ids=sorted(failover_replicate_scope_ids))
            job_logger.info(
                f'Replicated failover state for {len(failover_replicate_scope_ids)} '
                f'scope(s): {sorted(failover_replicate_scope_ids)}'
            )
        except PSUClientError as exc:
            job_logger.error(
                f'Failed to replicate failover state for scope(s) '
                f'{sorted(failover_replicate_scope_ids)}: {exc}'
            )

    DHCPServer.objects.filter(pk=server.pk).update(
        last_sync_at=timezone.now(),
        last_sync_error='',
    )


# ---------------------------------------------------------------------------
# Job classes
# ---------------------------------------------------------------------------

@system_job(interval=60)
class DHCPSyncJob(JobRunner):
    """Synchronise all DHCP servers with NetBox."""

    class Meta:
        name = 'Windows DHCP Sync'
        description = (
            'Pulls scope, lease, and reservation data from Windows DHCP servers '
            'via PowerShell Universal and updates NetBox IP Address objects.'
        )

    def __init__(self, job):
        super().__init__(job)
        _quiet_file_log(self.logger)

    @classmethod
    def enqueue(cls, *args, **kwargs):
        """
        Wrap JobRunner.enqueue so every enqueue path — @system_job startup,
        settings-change signal, view button, and the auto-reschedule inside
        JobRunner.handle()'s finally block — pulls queue, timeout, and
        interval from the plugin settings singleton without each caller
        having to pass them explicitly. setdefault preserves explicit overrides.
        """
        try:
            cfg = _load_settings()
            kwargs.setdefault('queue_name', cfg.sync_queue)
            kwargs.setdefault('job_timeout', cfg.sync_job_timeout)
            kwargs.setdefault('interval', cfg.sync_interval)
        except Exception:
            # Settings may not be loadable during initial migrations — fall through
            # with whatever the caller provided.
            pass
        return super().enqueue(*args, **kwargs)

    @classmethod
    def converge_schedule(cls, **enqueue_once_kwargs):
        """
        Collapse any duplicate pending/scheduled jobs of this name down to one,
        then optionally hand off to enqueue_once() to apply new kwargs (e.g. an
        updated interval) to the survivor.

        enqueue_once() alone cannot do this: it only ever inspects the single
        most-recently-created Job row for this name (Job.Meta.ordering =
        ['-created']), so once a second duplicate chain exists it's invisible
        to enqueue_once() and keeps perpetuating itself via JobRunner.handle()'s
        un-deduplicated reschedule. Per-instance .delete() (not queryset
        .delete()) is required here — Job.delete() is overridden to also
        cancel the RQ/Redis-side entry, and a bulk queryset delete skips that
        override, leaving the duplicate free to still fire from Redis.
        """
        from core.choices import JobStatusChoices
        from core.models import Job
        from django.db.models import F
        from django_pglocks import advisory_lock
        from netbox.constants import ADVISORY_LOCK_KEYS

        with advisory_lock(ADVISORY_LOCK_KEYS['job-schedules']):
            jobs = list(
                Job.objects.filter(
                    name=cls.name,
                    status__in=[JobStatusChoices.STATUS_PENDING, JobStatusChoices.STATUS_SCHEDULED],
                ).order_by(F('scheduled').asc(nulls_last=True), 'created')
            )
            for extra in jobs[1:]:
                extra.delete()

        if enqueue_once_kwargs:
            return cls.enqueue_once(**enqueue_once_kwargs)
        return jobs[0] if jobs else None

    def run(self, *args, **kwargs):
        import time

        from django.db import connection as _db_connection

        # However many duplicate "Windows DHCP Sync" chains exist right now
        # (repeated rqworker restarts, races between independent enqueue
        # paths, or any future bug), converge them to exactly this job on
        # every single run — see converge_schedule() above for why
        # enqueue_once() alone can't do this. Same convergence fix shipped in
        # v1.3.3 (110608c), silently reverted in v1.3.4 (c3d7d45); restored
        # here as an addition on top of (not a reversion of) that release's
        # @system_job/enqueue_once architecture.
        from core.choices import JobStatusChoices
        from core.models import Job
        from django_pglocks import advisory_lock
        from netbox.constants import ADVISORY_LOCK_KEYS

        with advisory_lock(ADVISORY_LOCK_KEYS['job-schedules']):
            siblings = list(
                Job.objects.filter(
                    name=DHCPSyncJob.name,
                    status__in=[JobStatusChoices.STATUS_PENDING, JobStatusChoices.STATUS_SCHEDULED],
                ).exclude(pk=self.job.pk)
            )
            for sibling in siblings:
                sibling.delete()
        if siblings:
            self.logger.warning(
                f'Converged {len(siblings)} duplicate scheduled/pending '
                f'"{DHCPSyncJob.name}" job(s) down to this run (job pk={self.job.pk}).'
            )

        _job_start = time.monotonic()
        run_errors: list = []

        _cfg = _load_settings()
        # Align Django's per-connection CONN_MAX_AGE with the configured job
        # timeout so the connection isn't recycled mid-job by close_old_connections().
        _db_connection.settings_dict['CONN_MAX_AGE'] = _cfg.sync_job_timeout

        from .api_client import PSUClient, PSUClientError
        from .constants import PSU_SCRIPT_VERSION
        from .models import DHCPFailover, DHCPServer
        from django.utils import timezone

        servers = list(DHCPServer.objects.all())
        if not servers:
            self.logger.info('No DHCP servers configured — nothing to sync.')
            return

        cfg = _cfg
        sync_ip_addresses = cfg.sync_ip_addresses
        push_reservations = cfg.push_reservations
        push_scope_info = cfg.push_scope_info
        sync_active_scopes_only = cfg.sync_active_scopes_only
        protect_tag = cfg.sync_protect_tag.slug if cfg.sync_protect_tag_id else ''
        update_client_id = cfg.sync_protect_update_client_id
        lease_status = cfg.lease_status
        reservation_status = cfg.reservation_status

        # Build protected prefix networks set for inheritance checking.
        protected_prefix_networks = set()
        if cfg.sync_protect_tag_id:
            import ipaddress as _ipmod
            from ipam.models import Prefix as _Prefix
            for p in _Prefix.objects.filter(tags__slug=protect_tag):
                try:
                    protected_prefix_networks.add(
                        _ipmod.ip_network(str(p.prefix), strict=False)
                    )
                except ValueError:
                    pass
            if protected_prefix_networks:
                self.logger.info(
                    f'Loaded {len(protected_prefix_networks)} protected prefix(es) '
                    f'via tag {protect_tag!r}'
                )

        self.logger.info(
            f'Starting DHCP sync: sync_ip_addresses={sync_ip_addresses} '
            f'push_reservations={push_reservations} push_scope_info={push_scope_info} '
            f'sync_active_scopes_only={sync_active_scopes_only} '
            f'protect_tag={protect_tag!r} update_client_id={update_client_id} '
            f'lease_status={lease_status!r} reservation_status={reservation_status!r}'
        )

        # ── Health check phase ─────────────────────────────────────────
        self.logger.info('=== Health Check Phase ===')
        now = timezone.now()
        for server in servers:
            if server.maintenance_mode:
                self.logger.info(f'Server {server.name}: health check skipped (maintenance mode)')
                continue
            try:
                result = PSUClient(server).ping_read()
                version = result.get('version', '')
                server.health_status = DHCPServer.HEALTH_HEALTHY
                server.last_health_check = now
                server.health_error = ''
                server.psu_script_version = version
                server.save(update_fields=[
                    'health_status', 'last_health_check', 'health_error', 'psu_script_version'
                ])
                if version and version != PSU_SCRIPT_VERSION:
                    self.logger.warning(
                        f'Server {server.name}: PSU script version mismatch '
                        f'(expected {PSU_SCRIPT_VERSION}, got {version})'
                    )
                else:
                    self.logger.info(f'Server {server.name}: healthy (PSU script v{version or "unknown"})')
            except PSUClientError as exc:
                server.health_status = DHCPServer.HEALTH_UNREACHABLE
                server.last_health_check = now
                server.health_error = str(exc)
                server.save(update_fields=['health_status', 'last_health_check', 'health_error'])
                self.logger.warning(f'Server {server.name}: unreachable — {exc}')
                run_errors.append(f'{server.name} unreachable: {exc}')

        # Reload so health_status is fresh for fallback map logic.
        server_map = {s.pk: s for s in DHCPServer.objects.all()}

        # ── Build secondary fallback map ───────────────────────────────
        # failover_pk → secondary server that should handle it as fallback.
        fallback_map = {}
        for failover in DHCPFailover.objects.select_related('primary_server', 'secondary_server'):
            primary = server_map.get(failover.primary_server_id)
            secondary = server_map.get(failover.secondary_server_id)
            if not primary or not secondary:
                continue
            primary_down = (
                primary.maintenance_mode
                or primary.health_status == DHCPServer.HEALTH_UNREACHABLE
            )
            secondary_ok = (
                not secondary.maintenance_mode
                and secondary.health_status == DHCPServer.HEALTH_HEALTHY
            )
            if primary_down and secondary_ok:
                fallback_map[failover.pk] = secondary
                self.logger.info(
                    f'Failover "{failover.name}": primary {primary.name} is down — '
                    f'routing through secondary {secondary.name}'
                )

        # ── Sync phase ─────────────────────────────────────────────────
        self.logger.info('=== Sync Phase ===')

        # Determine which fallback failovers each server should handle.
        # key: server_pk → set of fallback failover PKs
        server_fallbacks: dict = {s.pk: set() for s in server_map.values()}
        for failover_pk, secondary in fallback_map.items():
            server_fallbacks[secondary.pk].add(failover_pk)

        import signal

        with _change_logging():
            for server in server_map.values():
                if server.maintenance_mode:
                    self.logger.info(f'Skipping server {server.name}: maintenance mode enabled')
                    continue
                if server.health_status == DHCPServer.HEALTH_UNREACHABLE:
                    self.logger.info(f'Skipping server {server.name}: unreachable')
                    continue
                _remaining = signal.getitimer(signal.ITIMER_REAL)[0]
                self.logger.info(
                    f'Starting {server.name} — job elapsed: '
                    f'{time.monotonic() - _job_start:.1f}s, '
                    f'SIGALRM remaining: {_remaining:.1f}s'
                )
                try:
                    _sync_server(
                        self.logger, server, sync_ip_addresses, push_reservations, push_scope_info,
                        sync_active_scopes_only=sync_active_scopes_only,
                        protect_tag=protect_tag, update_client_id=update_client_id,
                        lease_status=lease_status, reservation_status=reservation_status,
                        fallback_failover_ids=server_fallbacks[server.pk],
                        protected_prefix_networks=protected_prefix_networks,
                    )
                except Exception as exc:
                    self.logger.error(f'Error syncing server {server.name}: {exc}')
                    try:
                        DHCPServer.objects.filter(pk=server.pk).update(last_sync_error=str(exc))
                    except Exception as db_exc:
                        self.logger.error(
                            f'Could not record sync error for {server.name} '
                            f'({type(db_exc).__name__}): {db_exc}'
                        )
                    run_errors.append(f'{server.name} sync error: {exc}')

        if run_errors:
            from core.exceptions import JobFailed
            self.job.error = '; '.join(run_errors)
            self.job.save(update_fields=['error'])
            raise JobFailed()


def _check_server_health(logger, job, server):
    """
    Ping the server and update its health bookkeeping either way. Shared by
    every single-server job (DHCPServerSyncJob, DHCPScopePushJob) so a server's
    health_status/last_health_check/psu_script_version stay fresh regardless of
    which kind of job last talked to it, and so that an unreachable server
    always surfaces as a failed job — not a silently-empty completed one.

    Raises JobFailed (after recording the error on `job`) if the server is
    unreachable. No fallback routing to a failover partner happens here —
    that's only computed at the whole-fleet DHCPSyncJob level, which is the
    only job with visibility into every server's health at once.
    """
    from django.utils import timezone

    from .api_client import PSUClient, PSUClientError
    from .constants import PSU_SCRIPT_VERSION
    from .models import DHCPServer

    now = timezone.now()
    try:
        result = PSUClient(server).ping_read()
        version = result.get('version', '')
        server.health_status = DHCPServer.HEALTH_HEALTHY
        server.last_health_check = now
        server.health_error = ''
        server.psu_script_version = version
        server.save(update_fields=[
            'health_status', 'last_health_check', 'health_error', 'psu_script_version'
        ])
        if version and version != PSU_SCRIPT_VERSION:
            logger.warning(
                f'PSU script version mismatch '
                f'(expected {PSU_SCRIPT_VERSION}, got {version})'
            )
        else:
            logger.info(f'Health check passed (PSU script v{version or "unknown"})')
    except PSUClientError as exc:
        server.health_status = DHCPServer.HEALTH_UNREACHABLE
        server.last_health_check = now
        server.health_error = str(exc)
        server.save(update_fields=['health_status', 'last_health_check', 'health_error'])
        logger.error(f'Server unreachable — {exc}')
        from core.exceptions import JobFailed
        job.error = f'{server.name} unreachable: {exc}'
        job.save(update_fields=['error'])
        raise JobFailed()


class DHCPServerSyncJob(JobRunner):
    """Sync a single DHCPServer on demand (enqueued by the Sync Now button)."""

    class Meta:
        name = 'Windows DHCP Server Sync'
        description = 'On-demand sync for a single Windows DHCP server.'

    def __init__(self, job):
        super().__init__(job)
        _quiet_file_log(self.logger)

    def run(self, *args, **kwargs):
        server_pk = kwargs.get('server_pk')
        if not server_pk:
            self.logger.error('No server_pk provided to job.')
            return

        from django.db import connection as _db_connection

        from .models import DHCPServer

        cfg = _load_settings()
        # Align CONN_MAX_AGE with the configured job timeout so the connection
        # isn't recycled mid-job by close_old_connections().
        _db_connection.settings_dict['CONN_MAX_AGE'] = cfg.sync_job_timeout

        try:
            server = DHCPServer.objects.get(pk=server_pk)
        except DHCPServer.DoesNotExist:
            self.logger.error(f'DHCPServer pk={server_pk} not found.')
            return

        # Single-server health check — fail immediately if unreachable.
        # No fallback routing for on-demand syncs.
        _check_server_health(self.logger, self.job, server)

        protect_tag = cfg.sync_protect_tag.slug if cfg.sync_protect_tag_id else ''
        protected_prefix_networks = set()
        if cfg.sync_protect_tag_id:
            import ipaddress as _ipmod
            from ipam.models import Prefix as _Prefix
            for p in _Prefix.objects.filter(tags__slug=protect_tag):
                try:
                    protected_prefix_networks.add(
                        _ipmod.ip_network(str(p.prefix), strict=False)
                    )
                except ValueError:
                    pass
        with _change_logging():
            _sync_server(
                self.logger, server,
                cfg.sync_ip_addresses, cfg.push_reservations, cfg.push_scope_info,
                sync_active_scopes_only=cfg.sync_active_scopes_only,
                protect_tag=protect_tag,
                update_client_id=cfg.sync_protect_update_client_id,
                lease_status=cfg.lease_status,
                reservation_status=cfg.reservation_status,
                protected_prefix_networks=protected_prefix_networks,
            )


def _push_scopes(job_logger, client, server, scope_pks):
    """
    Push exactly the given DHCPScope pks to `server` — no reconciliation of any
    other scope, lease, reservation, or exclusion on that server. Used by
    DHCPScopePushJob so that saving a handful of scopes doesn't trigger a full
    reconcile of a server that might have hundreds of unrelated scopes.

    Remote state is fetched via one list_scopes() call up front (same call
    _sync_server makes), not a per-scope GET /api/dhcp/scopes/:scope_id —
    that single-scope endpoint's ConvertTo-ScopeObject never includes
    router/failover_name (only the bulk list endpoint attaches those), so
    per-scope fetching made _push_scope() see a scope's router and failover
    membership as always unset and re-push them on every single run.
    """
    from django.utils import timezone

    from .api_client import PSUClientError
    from .models import DHCPScope

    try:
        remote_scopes = client.list_scopes()
    except PSUClientError as exc:
        job_logger.error(f'Failed to fetch scopes from {server.name}: {exc}')
        return

    remote_scope_map = {}
    for rs in remote_scopes:
        scope_id = rs.get('scope_id') or rs.get('ScopeId') or rs.get('network_address')
        if scope_id:
            remote_scope_map[scope_id] = rs

    failover_replicate_scope_ids = set()

    for scope in DHCPScope.objects.filter(pk__in=scope_pks).select_related('prefix', 'failover'):
        if scope.maintenance_mode:
            job_logger.info(f'Scope "{scope.name}": in maintenance mode — skipping')
            continue

        if scope.failover_id:
            if scope.failover.maintenance_mode:
                job_logger.info(
                    f'Scope "{scope.name}": failover "{scope.failover.name}" is in maintenance mode — skipping'
                )
                continue
            if not scope.failover.sync_enabled:
                job_logger.debug(
                    f'Scope "{scope.name}": failover "{scope.failover.name}" has sync disabled — skipping'
                )
                continue
            if server.pk != scope.failover.primary_server_id:
                # This job never targets the secondary directly — Windows
                # failover replication (triggered below) propagates the
                # change to it. There's no fallback-routing concept here
                # (unlike _sync_server's fallback_failover_ids) since this
                # job is only ever triggered by a direct scope save.
                continue
        elif scope.server_id:
            if scope.server_id != server.pk:
                continue
            if not server.sync_standalone_scopes:
                job_logger.debug(
                    f'Scope "{scope.name}": standalone scopes disabled on {server.name} — skipping'
                )
                continue
        else:
            job_logger.debug(f'Scope "{scope.name}": no server or failover assigned — skipping')
            continue

        try:
            scope_id = str(scope.prefix.prefix.network)
        except Exception:
            job_logger.warning(f'Could not determine network address for scope "{scope.name}" — skipping')
            continue

        remote = remote_scope_map.get(scope_id)

        if remote is None:
            scope_pushed = _push_scope(job_logger, client, scope)
        else:
            scope_pushed = _push_scope(job_logger, client, scope, remote=remote, scope_id=scope_id)

        exclusions_changed = _sync_exclusions(job_logger, client, scope, scope_id)
        if scope.failover_id and (scope_pushed or exclusions_changed):
            failover_replicate_scope_ids.add(scope_id)

        DHCPScope.objects.filter(pk=scope.pk).update(last_sync_at=timezone.now())

    if failover_replicate_scope_ids:
        try:
            client.replicate_failover(scope_ids=sorted(failover_replicate_scope_ids))
            job_logger.info(
                f'Replicated failover state for {len(failover_replicate_scope_ids)} '
                f'scope(s): {sorted(failover_replicate_scope_ids)}'
            )
        except PSUClientError as exc:
            job_logger.error(
                f'Failed to replicate failover state for scope(s) '
                f'{sorted(failover_replicate_scope_ids)}: {exc}'
            )


class DHCPScopePushJob(JobRunner):
    """
    Push specific DHCPScope(s) to a single server — triggered by saving a scope
    when push_scope_info=True. Unlike DHCPServerSyncJob, this never reconciles
    the rest of the server's scopes, leases, reservations, or exclusions for
    scopes not in scope_pks — it exists specifically so that editing a handful
    of scopes doesn't trigger a full reconcile of a server that might have
    hundreds of unrelated scopes.
    """

    class Meta:
        name = 'Windows DHCP Scope Push'
        description = 'Push specific scope(s) to a Windows DHCP server without a full server reconcile.'

    def __init__(self, job):
        super().__init__(job)
        _quiet_file_log(self.logger)

    def run(self, *args, **kwargs):
        server_pk = kwargs.get('server_pk')
        scope_pks = kwargs.get('scope_pks') or []
        if not server_pk or not scope_pks:
            self.logger.error('No server_pk/scope_pks provided to job.')
            return

        from .api_client import PSUClient
        from .models import DHCPServer

        try:
            server = DHCPServer.objects.get(pk=server_pk)
        except DHCPServer.DoesNotExist:
            self.logger.error(f'DHCPServer pk={server_pk} not found.')
            return

        if server.maintenance_mode:
            self.logger.info(f'Skipping server {server.name}: maintenance mode enabled')
            return

        _check_server_health(self.logger, self.job, server)

        client = PSUClient(server)
        _push_scopes(self.logger, client, server, scope_pks)


def _deconfigure_and_delete_scope(job_logger, client, scope_id, failover_name=None):
    """
    Remove `scope_id` from the DHCP server, deconfiguring it from its failover
    relationship first if it has one. Remove-DhcpServerv4FailoverScope (driven
    via update_scope's failover.remove) already deletes the scope from the
    partner server as part of deconfiguring it, so this never needs to touch
    the secondary directly — only ever call it against the primary (or a
    standalone scope's own server).

    Swallows and logs any error at each step (matching _push_scope's broad
    `except Exception`) so one failure doesn't abort a larger batch of
    deletes — the NetBox-side delete has already happened regardless of
    whether the remote call succeeds.
    """
    if failover_name:
        try:
            client.update_scope(scope_id, {'failover': {'remove': failover_name}})
            job_logger.info(f'Scope {scope_id}: deconfigured from failover {failover_name!r}')
        except Exception as exc:
            job_logger.warning(
                f'Scope {scope_id}: failed to deconfigure from failover {failover_name!r}: {exc}'
            )

    try:
        client.delete_scope(scope_id)
        job_logger.info(f'Scope {scope_id}: deleted from server')
    except Exception as exc:
        job_logger.warning(f'Scope {scope_id}: failed to delete from server: {exc}')


def _delete_scopes(job_logger, client, server, deletes):
    """
    Delete exactly the given scopes from the server. `deletes` is a list of
    dicts snapshotted by signals.py's pre_delete receiver — by the time this
    job runs, the DHCPScope row itself is already gone, so everything needed
    (scope_id, failover_name, maintenance-mode flags) was captured up front:
        {'scope_id': ..., 'scope_name': ..., 'failover_name': ..., 'maintenance_mode': bool}

    failover.sync_enabled / server.sync_standalone_scopes are re-checked here
    against the live DB rather than snapshotted at pre_delete time — unlike
    the scope itself, the DHCPFailover/DHCPServer rows still exist when this
    job runs, so this mirrors _push_scopes checking them fresh rather than
    trusting a point-in-time snapshot.
    """
    from .models import DHCPFailover

    for item in deletes:
        scope_id = item['scope_id']
        scope_name = item.get('scope_name', scope_id)
        if item.get('maintenance_mode'):
            job_logger.info(f'Scope "{scope_name}": in maintenance mode — skipping delete')
            continue

        failover_name = item.get('failover_name')
        if failover_name:
            failover = DHCPFailover.objects.filter(name=failover_name).first()
            if failover is not None and not failover.sync_enabled:
                job_logger.debug(
                    f'Scope "{scope_name}": failover "{failover_name}" has sync disabled — skipping delete'
                )
                continue
        elif not server.sync_standalone_scopes:
            job_logger.debug(
                f'Scope "{scope_name}": standalone scopes disabled on {server.name} — skipping delete'
            )
            continue

        _deconfigure_and_delete_scope(job_logger, client, scope_id, failover_name=failover_name)


class DHCPScopeDeleteJob(JobRunner):
    """
    Delete specific scope(s) from a single server — triggered by deleting a
    DHCPScope when push_scope_info=True. Mirrors DHCPScopePushJob's shape:
    batched per server via signals.py's pre_delete accumulator, scoped only
    to what was actually deleted.
    """

    class Meta:
        name = 'Windows DHCP Scope Delete'
        description = 'Delete specific scope(s) from a Windows DHCP server.'

    def __init__(self, job):
        super().__init__(job)
        _quiet_file_log(self.logger)

    def run(self, *args, **kwargs):
        server_pk = kwargs.get('server_pk')
        deletes = kwargs.get('deletes') or []
        if not server_pk or not deletes:
            self.logger.error('No server_pk/deletes provided to job.')
            return

        from .api_client import PSUClient
        from .models import DHCPServer

        try:
            server = DHCPServer.objects.get(pk=server_pk)
        except DHCPServer.DoesNotExist:
            self.logger.error(f'DHCPServer pk={server_pk} not found.')
            return

        if server.maintenance_mode:
            self.logger.info(f'Skipping server {server.name}: maintenance mode enabled')
            return

        _check_server_health(self.logger, self.job, server)

        client = PSUClient(server)
        _delete_scopes(self.logger, client, server, deletes)


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

        cfg = _load_settings()

        from .import_logic import run_import
        self.logger.info(f'Starting import from {server.name} ({server.hostname})')
        with _change_logging():
            results = run_import(server, active_only=cfg.sync_active_scopes_only)

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


# ===========================================================================
# PSU Script Update Job
# ===========================================================================

def _parse_psu_script():
    """
    Parse the bundled dhcp_api_endpoints.ps1 and return a list of endpoint dicts:
        [{'url': str, 'method': str, 'script_block': str}, ...]

    'script_block' is the combined H-helpers + inner endpoint code that PSU stores.
    """
    import re
    from importlib.resources import files

    content = files('netbox_windows_dhcp').joinpath('psu/dhcp_api_endpoints.ps1').read_text(encoding='utf-8')

    # Extract $H heredoc content (between $H = @'\n ... \n'@)
    h_match = re.search(r"\$H = @'\n(.*?)\n'@", content, re.DOTALL)
    if not h_match:
        raise ValueError('Could not locate $H heredoc in dhcp_api_endpoints.ps1')
    h_content = h_match.group(1) + '\n'  # trailing newline matches runtime behaviour

    # Extract each New-PSUEndpoint call
    # Pattern: New-PSUEndpoint -Url 'URL' -Method METHOD @_ep... -Endpoint ([scriptblock]::Create($H + {
    #              inner code
    #          }.ToString()))
    endpoint_pattern = re.compile(
        r"New-PSUEndpoint\s+-Url\s+'([^']+)'\s+-Method\s+(\w+)\s+"
        r"@_ep\w+\s+-Endpoint\s+\(\[scriptblock\]::Create\(\$H\s*\+\s*\{(.*?)\}\.ToString\(\)\)\)",
        re.DOTALL,
    )
    endpoints = []
    for m in endpoint_pattern.finditer(content):
        url = m.group(1)
        method = m.group(2).upper()
        inner_code = m.group(3)
        # script_block is what PSU stores: H content + endpoint code
        script_block = h_content + inner_code
        endpoints.append({'url': url, 'method': method, 'script_block': script_block})
    return endpoints


class DHCPPSUUpdateJob(JobRunner):
    """Push updated PSU script endpoint definitions to a DHCP server."""

    class Meta:
        name = 'Windows DHCP PSU Script Update'
        description = 'Pushes updated PSU endpoint scriptBlocks to a DHCP server.'

    def run(self, *args, **kwargs):
        server_pk = kwargs.get('server_pk')
        if not server_pk:
            self.logger.error('No server_pk provided to job.')
            return

        from django.utils import timezone

        from .api_client import PSUClient, PSUClientError
        from .constants import PSU_SCRIPT_VERSION
        from .models import DHCPServer

        try:
            server = DHCPServer.objects.get(pk=server_pk)
        except DHCPServer.DoesNotExist:
            self.logger.error(f'DHCPServer pk={server_pk} not found.')
            return

        self.logger.info(f'Starting PSU script update for {server.name} ({server.hostname})')

        # Parse bundled script
        try:
            defined_endpoints = _parse_psu_script()
        except Exception as exc:
            self.logger.error(f'Failed to parse dhcp_api_endpoints.ps1: {exc}')
            return

        self.logger.info(f'Parsed {len(defined_endpoints)} endpoint definition(s) from PS1 file')

        client = PSUClient(server)

        # Fetch current PSU endpoint records
        try:
            current_eps = client.get_dhcp_endpoints()
        except PSUClientError as exc:
            self.logger.error(f'Failed to fetch current PSU endpoints: {exc}')
            return

        self.logger.info(f'Found {len(current_eps)} existing /api/dhcp/ endpoint record(s) in PSU')

        # Build lookup: (url, method) → endpoint record
        current_map = {
            (ep.get('url', ''), m): ep
            for ep in current_eps
            for m in ([ep['method']] if isinstance(ep.get('method'), str) else ep.get('method', []))
        }

        defined_keys = {(d['url'], d['method']) for d in defined_endpoints}
        current_keys = set(current_map.keys())

        # Update existing endpoints
        updated = 0
        for defn in defined_endpoints:
            key = (defn['url'], defn['method'])
            if key in current_map:
                ep_record = dict(current_map[key])
                ep_record['scriptBlock'] = defn['script_block']
                try:
                    client.update_endpoint(ep_record)
                    self.logger.info(f'Updated endpoint {defn["method"]} {defn["url"]}')
                    updated += 1
                except PSUClientError as exc:
                    self.logger.error(f'Failed to update {defn["method"]} {defn["url"]}: {exc}')

        # Create new endpoints (in defined but not in PSU)
        created = 0
        for defn in defined_endpoints:
            key = (defn['url'], defn['method'])
            if key not in current_keys:
                new_ep = {
                    'url': defn['url'],
                    'method': [defn['method']],
                    'scriptBlock': defn['script_block'],
                    'authentication': True,
                    'role': ['DHCPWriter'] if defn['method'] != 'GET' else ['DHCPReader', 'DHCPWriter'],
                    'regEx': False,
                    'errorAction': 0,
                    'timeout': 30,
                    'disabled': False,
                }
                try:
                    client.create_endpoint(new_ep)
                    self.logger.info(f'Created endpoint {defn["method"]} {defn["url"]}')
                    created += 1
                except PSUClientError as exc:
                    self.logger.error(f'Failed to create {defn["method"]} {defn["url"]}: {exc}')

        # Delete removed endpoints (in PSU but not in defined)
        deleted = 0
        for key in (current_keys - defined_keys):
            ep_record = current_map[key]
            try:
                client.delete_endpoint(ep_record['id'])
                self.logger.info(f'Deleted endpoint {key[1]} {key[0]}')
                deleted += 1
            except PSUClientError as exc:
                self.logger.error(f'Failed to delete {key[1]} {key[0]}: {exc}')

        self.logger.info(
            f'Endpoints: {updated} updated, {created} created, {deleted} deleted'
        )

        # Restart endpoint definitions in PSU
        try:
            client.restart_endpoints()
            self.logger.info('PSU endpoints restarted successfully')
        except PSUClientError as exc:
            self.logger.error(f'Failed to restart PSU endpoints: {exc}')
            return

        # Verify new version is live
        import time
        time.sleep(2)  # give PSU a moment to reload
        try:
            result = client.ping_read()
            new_version = result.get('version', '')
            now = timezone.now()
            server.psu_script_version = new_version
            server.last_health_check = now
            server.health_status = DHCPServer.HEALTH_HEALTHY
            server.health_error = ''
            server.save(update_fields=['psu_script_version', 'last_health_check', 'health_status', 'health_error'])
            if new_version == PSU_SCRIPT_VERSION:
                self.logger.info(f'PSU scripts updated to v{new_version} ✓')
            else:
                self.logger.warning(
                    f'PSU scripts updated but version mismatch: '
                    f'got {new_version!r}, expected {PSU_SCRIPT_VERSION!r}'
                )
        except PSUClientError as exc:
            self.logger.error(f'Health check after update failed: {exc}')
            server.health_status = DHCPServer.HEALTH_UNREACHABLE
            server.health_error = str(exc)
            server.save(update_fields=['health_status', 'health_error'])
