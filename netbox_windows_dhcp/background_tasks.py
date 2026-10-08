"""
Background sync job for netbox-windows-dhcp.

Sync logic:
  For each DHCPServer:
    1. Fetch /scopes from PSU.
    2. If sync_ip_addresses or push_reservations is True, bulk-fetch leases/
       reservations for every scope on the server in one call each (not one call
       per scope — see _sync_server for the failure-handling details).
    3. Match returned scopes against DHCPScope objects (by network address).
    4. If sync_ip_addresses == True:
         - Look up this scope's leases + reservations from the bulk fetch above.
         - Update/create NetBox IPAddress objects with status 'dhcp' (lease) or
           'reserved' (reservation). For 'reserved' IPs without a dhcp_client_id,
           update the client_id from a discovered lease but preserve the status and dns_name.
         - Store lease hostname in DHCPLeaseInfo (non-changelog side-table).
    5. If push_reservations == True:
         - Push NetBox "reserved" IPs with a dhcp_client_id within scope ranges to DHCP server.
    6. If push_scope_info == True:
         - Push scope config to DHCP server.
"""

import json
import logging
import time
import uuid
from contextlib import contextmanager
from typing import Optional

from netbox.jobs import JobRunner

try:
    from django_pg_utils import advisory_lock  # NetBox 4.7+
except ImportError:
    from django_pglocks import advisory_lock  # NetBox 4.5-4.6

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


def _apply_sync_log_level(job_logger):
    """
    Raise the job logger's effective level from NetBox core's hardcoded DEBUG to the
    configured sync_log_level, so filtered-out calls never become log records (and
    never get appended to the job's log_entries array written to the DB).
    """
    cfg = _load_settings()
    job_logger.setLevel(getattr(logging, cfg.sync_log_level, logging.DEBUG))


def _service_user():
    """Return the DHCP-Sync-Service account, or None if not yet created (e.g. before first migrate)."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        return User.objects.get(username='DHCP-Sync-Service')
    except User.DoesNotExist:
        return None


@contextmanager
def _change_logging():
    """
    Run the block under NetBox's event_tracking() with a fake request attributed to the
    DHCP-Sync-Service account, so changes get ObjectChange records AND fire event rules
    (webhooks, scripts, notifications) the same way a UI/API edit would.

    Always uses the service account regardless of who triggered the job, so that sync-driven
    changes are clearly distinguished from human edits in the audit trail. NetBox's
    handle_changed_object (core/signals.py) returns immediately if current_request is None,
    which is always the case in background jobs — this context manager provides the fake
    request that enables change logging. The request carries the attributes
    copy_safe_request() reads when an event rule matches (same shape as NetBox's runscript).

    Also sets the plugin-write flag (utils.plugin_write_active()) for the block, so the
    plugin's own signal handlers can tell the sync's writes apart from a user's.
    """
    from .utils import plugin_write

    with plugin_write():
        with _service_user_event_tracking():
            yield


@contextmanager
def _service_user_event_tracking():
    """The event_tracking() half of _change_logging() — see its docstring."""
    from netbox.context_managers import event_tracking
    from utilities.request import NetBoxFakeRequest

    service_user = _service_user()
    if service_user is None:
        # Service account not yet created (e.g. before first migrate). Skip rather than crash.
        yield
        return

    request = NetBoxFakeRequest({
        'META': {},
        'COOKIES': {},
        'POST': {},
        'GET': {},
        'FILES': {},
        'user': service_user,
        'method': 'POST',
        'path': '',
        'id': uuid.uuid4(),
    })

    # event_tracking() flushes queued events and clears its context vars only on a clean
    # exit. Changes are committed as they happen (no wrapping transaction), so on an error
    # we still exit it cleanly — flushing events for the changes already saved — then re-raise.
    pending = None
    with event_tracking(request):
        try:
            yield
        except BaseException as exc:
            pending = exc
    if pending is not None:
        raise pending


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
        # lease info is informational — never let it abort the main sync
        from django.db import connection
        connection.close()  # reset a connection possibly corrupted by a mid-query job timeout


INVALID_HOSTNAME_TAG_SLUG = 'invalid-client-hostname'


def _clean_dns_name(raw: str) -> tuple[str, bool]:
    """
    Convert a hostname reported by the DHCP server into a NetBox dns_name.
    Returns (dns_name, invalid):
      No hostname                              → ('', False)
      Passes NetBox's DNSValidator (lowercased) → (name, False)
      Anything else (spaces, backslashes, >255) → ('', True)
    Invalid names are never rewritten into a "fixed" form — a made-up name would not
    resolve in DNS and would mislead downstream consumers. The raw value is kept in
    DHCPLeaseInfo.lease_hostname and the IP is tagged INVALID_HOSTNAME_TAG_SLUG.
    """
    if not raw:
        return '', False
    from django.core.exceptions import ValidationError
    from ipam.validators import DNSValidator

    name = raw.lower()
    if len(name) > 255:
        return '', True
    try:
        DNSValidator(name)
    except ValidationError:
        return '', True
    return name, False


def _invalid_hostname_tag():
    """Return the invalid-client-hostname Tag, creating it if needed."""
    from extras.models import Tag
    tag, _ = Tag.objects.get_or_create(
        slug=INVALID_HOSTNAME_TAG_SLUG,
        defaults={
            'name': 'Invalid Client Hostname',
            'color': 'ff9800',
            'description': 'DHCP client reported a hostname NetBox does not accept as a DNS name '
                           '— see Lease Hostname for the raw value',
        },
    )
    return tag


def _reservation_description(res):
    """
    The Description from a PSU reservation record, trimmed to IPAddress.description's
    length — or None when the record has no description key, so it never wipes NetBox.
    """
    for key in ('description', 'Description'):
        if key in res:
            return (res.get(key) or '')[:200]
    return None


def _sync_scope_ips(job_logger, scope, leases: list, reservations: list,
                    protect_tag: str = '', update_client_id: bool = False,
                    lease_status: str = 'dhcp', reservation_status: str = 'reserved',
                    protected_prefix_networks=frozenset(),
                    push_reservations: bool = False):
    """
    Synchronize IP addresses for a single scope using one bulk DB read per scope.

    Replaces the old per-IP _upsert_ip_address pattern: one
    IPAddress.objects.filter(net_contained_or_equal) instead of one query per IP.
    DHCPLeaseInfo rows are written via bulk_create/bulk_update at the end.

    Pass 1 — reservations (higher precedence). With push_reservations off, the
             reservation's Description is pulled into the IP's description too.
             With it on, NetBox wins: server reservations never create or update
             NetBox IPs (see _reconcile_reservations); only lease info is recorded.
    Pass 2 — leases (reservations in use are listed here too, as ActiveReservation)
    Pass 3 — bulk upsert DHCPLeaseInfo. Active means the server has a lease on the IP
             now: a reservation with no lease, or an IP with lease info that the server
             no longer reports, is written as inactive with no expiration.
    Pass 4 — cleanup stale IPs (only in the scope prefix's VRF)

    Cleanup contracts:
      Sync-protected IPs (tag or prefix) → never deleted or downgraded.
      push_reservations=True → reservation-status IPs are never touched, anywhere.
      Inside the start/end range and outside every exclusion, the server wins:
        the server has a reservation → kept;
        the server has only a lease → kept (a reservation-status IP is downgraded);
        neither → deleted, whatever its status (hand-made and device-assigned too).
      Outside the range, or inside an exclusion — only lease/reservation-status IPs
      with a DHCPLeaseInfo row (DHCP-managed) are cleaned up; hand-made IPs are left alone:
        reservation-status, no reservation + lease → downgrade; neither → delete.
        lease-status, no lease → delete.
    """
    import ipaddress as _ipmod
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime
    from ipam.models import IPAddress
    from .models import DHCPLeaseInfo

    if not scope.prefix_id:
        return 0, 0  # no prefix, so no NetBox IPs to sync

    prefix_cidr = str(scope.prefix.prefix)
    prefix_len = scope.prefix.prefix.prefixlen
    # The scope only ever sees IPs in its prefix's VRF (None = global), and new IPs are
    # created there. See the dhcp_fix_ip_vrf command for IPs created without a VRF.
    prefix_vrf_id = scope.prefix.vrf_id
    # New IPs inherit the prefix's tenant. Existing IPs are never re-tenanted by sync —
    # see the dhcp_apply_prefix_tenant management command for an on-demand backfill.
    prefix_tenant_id = scope.prefix.tenant_id

    # One bulk read — all IPs in this scope (any status).
    existing: dict[str, IPAddress] = {}
    for ip_obj in IPAddress.objects.filter(
        address__net_contained_or_equal=prefix_cidr, vrf_id=prefix_vrf_id,
    ).prefetch_related('tags'):
        existing[str(ip_obj.address.ip)] = ip_obj

    # Fetch existing DHCPLeaseInfo — used both for cleanup gating and bulk update.
    _existing_lease_info: dict[int, DHCPLeaseInfo] = {
        li.ip_address_id: li
        for li in DHCPLeaseInfo.objects.filter(
            ip_address__address__net_contained_or_equal=prefix_cidr,
            ip_address__vrf_id=prefix_vrf_id,
        )
    }
    dhcp_managed_ids = set(_existing_lease_info.keys())

    def _tag_protected(ip_obj) -> bool:
        return bool(protect_tag and ip_obj is not None and protect_tag in ip_obj.tags.slugs())

    def _prefix_protected(ip_str: str) -> bool:
        if not protected_prefix_networks:
            return False
        try:
            addr = _ipmod.ip_address(ip_str)
            return any(addr in net for net in protected_prefix_networks)
        except ValueError:
            return False

    # IPs whose client ID the sync changed to a different device's (see _client_changed).
    # Pass 3 resets their lease info's state_changed; a change after Pass 3 (a Pass 4
    # downgrade) is applied by _flush_late_client_changes instead.
    client_changed_pks: set[int] = set()
    _late_client_changed: set[int] = set()
    _pass3_done = []

    def _client_changed(ip_obj, stored, new) -> None:
        """Record ip_obj as having a new device on it, if `stored` and `new` are different IDs."""
        from .utils import normalize_client_id
        stored_norm = normalize_client_id(stored)
        # A first-time fill (nothing stored) isn't a different device.
        if stored_norm and stored_norm != normalize_client_id(new):
            client_changed_pks.add(ip_obj.pk)
            if _pass3_done:
                _late_client_changed.add(ip_obj.pk)

    _hostname_tag = []  # lazily-resolved invalid-client-hostname Tag (only when needed)

    def _get_hostname_tag():
        if not _hostname_tag:
            _hostname_tag.append(_invalid_hostname_tag())
        return _hostname_tag[0]

    def _create_ip(ip_str: str, status: str, raw_name: str, client_id: str, description=None):
        dns_name, dns_invalid = _clean_dns_name(raw_name)
        ip_obj = IPAddress(address=f'{ip_str}/{prefix_len}')
        ip_obj.vrf_id = prefix_vrf_id
        ip_obj.status = status
        ip_obj.dns_name = dns_name
        ip_obj.tenant_id = prefix_tenant_id
        if description:
            ip_obj.description = description
        if client_id:
            ip_obj.custom_field_data['dhcp_client_id'] = client_id
        ip_obj.save()
        reasons = ['new', f'status={status!r}']
        if prefix_vrf_id:
            reasons.append(f'vrf_id={prefix_vrf_id}')
        if prefix_tenant_id:
            reasons.append(f'tenant_id={prefix_tenant_id}')
        if dns_invalid:
            ip_obj.tags.add(_get_hostname_tag())
            reasons.append(f'invalid hostname {raw_name!r}')
        existing[ip_str] = ip_obj
        job_logger.debug(f'Created IP {ip_str} [{", ".join(reasons)}]')
        return ip_obj

    def _update_ip(ip_obj, ip_str: str, status: str, raw_name: str, client_id: str,
                   description=None) -> bool:
        """
        Diff the server's view against ip_obj and apply any differences. dns_name is
        always set to the cleaned hostname — including blank — so stale or invalid
        names are cleared. description is only compared when given (not None).
        Returns True if anything was written.
        """
        dns_name, dns_invalid = _clean_dns_name(raw_name)
        stored_client_id = ip_obj.custom_field_data.get('dhcp_client_id')
        has_tag = any(t.slug == INVALID_HOSTNAME_TAG_SLUG for t in ip_obj.tags.all())

        fields_changed: set[str] = set()
        change_reasons = []
        if ip_obj.status != status:
            change_reasons.append(f'status {ip_obj.status!r}→{status!r}')
            fields_changed.add('status')
        if ip_obj.dns_name != dns_name:
            change_reasons.append(f'dns_name {ip_obj.dns_name!r}→{dns_name!r}')
            fields_changed.add('dns_name')
        if client_id and stored_client_id != client_id:
            change_reasons.append(f'dhcp_client_id {stored_client_id!r}→{client_id!r}')
            fields_changed.add('custom_field_data')
        if description is not None and ip_obj.description != description:
            change_reasons.append(f'description {ip_obj.description!r}→{description!r}')
            fields_changed.add('description')
        tag_changed = has_tag != dns_invalid
        if tag_changed:
            change_reasons.append(
                f'invalid hostname {raw_name!r}' if dns_invalid
                else f'removed tag {INVALID_HOSTNAME_TAG_SLUG!r}'
            )

        if not fields_changed and not tag_changed:
            job_logger.debug(f'IP {ip_str} already up-to-date (status={status!r})')
            return False

        ip_obj.snapshot()  # before any mutation, so the changelog pre-change data is correct
        ip_obj.status = status
        ip_obj.dns_name = dns_name
        if 'description' in fields_changed:
            ip_obj.description = description
        if 'custom_field_data' in fields_changed:
            _client_changed(ip_obj, stored_client_id, client_id)
            ip_obj.custom_field_data['dhcp_client_id'] = client_id
        # last_updated is auto_now, which Django only writes when it's listed in update_fields.
        # Also saved on a tag-only change, matching a UI edit.
        ip_obj.save(update_fields=fields_changed | {'last_updated'})
        if tag_changed:
            if dns_invalid:
                ip_obj.tags.add(_get_hostname_tag())
            else:
                ip_obj.tags.remove(_get_hostname_tag())
        job_logger.debug(f'Updated IP {ip_str} [{", ".join(change_reasons)}]')
        return True

    # lease_info_map: {ip_pk: DHCPLeaseInfo}; lease pass overwrites reservation pass.
    lease_info_map: dict[int, DHCPLeaseInfo] = {}
    reservation_ips: set[str] = set()
    # lease_ips: {ip_str: (hostname, client_id)} — kept so a Pass 4 downgrade can apply the lease's data.
    lease_ips: dict[str, tuple[str, str]] = {}
    _n_processed = 0
    _n_changed = 0

    # ------------------------------------------------------------------ #
    # Pass 1 — reservations (higher precedence)                           #
    # ------------------------------------------------------------------ #
    for res in reservations:
        ip_str = res.get('ip_address') or res.get('IPAddress')
        if not ip_str:
            continue
        client_id = res.get('client_id') or res.get('ClientId') or ''
        name = res.get('name') or res.get('Name') or ''
        # Server wins for the description only while NetBox doesn't push reservations.
        description = None if push_reservations else _reservation_description(res)
        reservation_ips.add(ip_str)
        _n_processed += 1

        try:
            ip_obj = existing.get(ip_str)
            tag_prot = _tag_protected(ip_obj)
            pfx_prot = _prefix_protected(ip_str)

            if tag_prot or pfx_prot:
                if pfx_prot and ip_obj is None:
                    job_logger.debug(f'Protected IP {ip_str}: creation skipped (in protected prefix)')
                    continue
                source = 'sync-protect tag' if tag_prot else 'protected prefix'
                job_logger.debug(f'Protected IP {ip_str}: skipped by {source}')
                if ip_obj is not None:
                    lease_info_map[ip_obj.pk] = DHCPLeaseInfo(
                        ip_address_id=ip_obj.pk, lease_hostname=name or '',
                        active=False, lease_expiration=None,
                    )
                continue

            if push_reservations:
                # NetBox wins for reservations: the server's copy never creates or
                # overwrites a NetBox IP. Only lease info is recorded for an existing IP.
                if ip_obj is None:
                    continue
            elif ip_obj is None:
                ip_obj = _create_ip(ip_str, reservation_status, name, client_id, description)
                _n_changed += 1
            elif _update_ip(ip_obj, ip_str, reservation_status, name, client_id, description):
                _n_changed += 1

            # Inactive until Pass 2 finds a lease for it (a reservation in use is listed
            # among the leases, as ActiveReservation).
            lease_info_map[ip_obj.pk] = DHCPLeaseInfo(
                ip_address_id=ip_obj.pk, lease_hostname=name or '',
                active=False, lease_expiration=None,
            )

        except Exception as exc:
            job_logger.warning(f'Failed to sync IP {ip_str}: {exc}', exc_info=True)
            from django.db import connection
            connection.close()

    # ------------------------------------------------------------------ #
    # Pass 2 — leases                                                     #
    # ------------------------------------------------------------------ #
    for lease in leases:
        ip_str = lease.get('ip_address') or lease.get('IPAddress')
        if not ip_str:
            continue
        client_id = lease.get('client_id') or lease.get('ClientId') or ''
        hostname = lease.get('hostname') or lease.get('HostName') or ''
        expiry_str = lease.get('lease_expiry') or lease.get('LeaseExpiry') or ''
        lease_ips[ip_str] = (hostname, client_id)
        _n_processed += 1

        lease_expiration = None
        if expiry_str:
            try:
                lease_expiration = parse_datetime(str(expiry_str))
                if lease_expiration and timezone.is_naive(lease_expiration):
                    lease_expiration = timezone.make_aware(lease_expiration)
            except Exception:
                pass

        try:
            ip_obj = existing.get(ip_str)
            tag_prot = _tag_protected(ip_obj)
            pfx_prot = _prefix_protected(ip_str)

            if tag_prot or pfx_prot:
                if tag_prot and update_client_id and client_id:
                    stored = ip_obj.custom_field_data.get('dhcp_client_id')
                    if client_id != stored:
                        ip_obj.snapshot()
                        _client_changed(ip_obj, stored, client_id)
                        ip_obj.custom_field_data['dhcp_client_id'] = client_id
                        ip_obj.save(update_fields=['custom_field_data', 'last_updated'])
                        _n_changed += 1
                        job_logger.debug(
                            f'Updated dhcp_client_id on protected IP {ip_str} from lease '
                            f'(client_id={client_id})'
                        )
                    else:
                        job_logger.debug(f'Protected IP {ip_str}: dhcp_client_id already up-to-date')
                else:
                    source = 'sync-protect tag' if tag_prot else 'protected prefix'
                    job_logger.debug(f'Protected IP {ip_str}: skipped by {source}')
                if ip_obj is not None:
                    lease_info_map[ip_obj.pk] = DHCPLeaseInfo(
                        ip_address_id=ip_obj.pk, lease_hostname=hostname or '',
                        active=True, lease_expiration=lease_expiration,
                    )
                continue

            # Reservation-status precedence: don't overwrite status or dns_name from a lease.
            if ip_obj is not None and ip_obj.status == reservation_status:
                stored_client_id = ip_obj.custom_field_data.get('dhcp_client_id')
                if client_id and not stored_client_id:
                    ip_obj.snapshot()
                    ip_obj.custom_field_data['dhcp_client_id'] = client_id
                    ip_obj.save(update_fields=['custom_field_data', 'last_updated'])
                    _n_changed += 1
                    job_logger.debug(
                        f'Updated dhcp_client_id on reserved IP {ip_str} from discovered lease '
                        f'(client_id={client_id})'
                    )
                lease_info_map[ip_obj.pk] = DHCPLeaseInfo(
                    ip_address_id=ip_obj.pk, lease_hostname=hostname or '',
                    active=True, lease_expiration=lease_expiration,
                )
                continue

            if ip_obj is None:
                ip_obj = _create_ip(ip_str, lease_status, hostname, client_id)
                _n_changed += 1
            elif _update_ip(ip_obj, ip_str, lease_status, hostname, client_id):
                _n_changed += 1

            lease_info_map[ip_obj.pk] = DHCPLeaseInfo(
                ip_address_id=ip_obj.pk, lease_hostname=hostname or '',
                active=True, lease_expiration=lease_expiration,
            )

        except Exception as exc:
            job_logger.warning(f'Failed to sync IP {ip_str}: {exc}', exc_info=True)
            from django.db import connection
            connection.close()

    # Any other IP with lease info wasn't reported by the server this run, so it has no
    # lease now (e.g. a protected IP, or a reservation NetBox keeps). Only its lease info
    # changes; Pass 4 decides whether the IP itself stays.
    for pk, existing_li in _existing_lease_info.items():
        if pk not in lease_info_map and (existing_li.active or existing_li.lease_expiration):
            lease_info_map[pk] = DHCPLeaseInfo(
                ip_address_id=pk, lease_hostname=existing_li.lease_hostname,
                active=False, lease_expiration=None,
            )

    # ------------------------------------------------------------------ #
    # Pass 3 — bulk upsert DHCPLeaseInfo                                  #
    # ------------------------------------------------------------------ #
    if lease_info_map:
        try:
            to_create = []
            to_update = []
            now = timezone.now()
            for pk, li in lease_info_map.items():
                if pk in _existing_lease_info:
                    existing_li = _existing_lease_info[pk]
                    # The state's clock restarts when Active flips or a different device
                    # has the IP; a renewal changes neither, so the clock keeps running.
                    if (existing_li.state_changed is None or existing_li.active != li.active
                            or pk in client_changed_pks):
                        existing_li.state_changed = now
                    existing_li.lease_hostname = li.lease_hostname
                    existing_li.active = li.active
                    existing_li.lease_expiration = li.lease_expiration
                    to_update.append(existing_li)
                else:
                    li.state_changed = now
                    to_create.append(li)
            if to_create:
                DHCPLeaseInfo.objects.bulk_create(to_create)
            if to_update:
                DHCPLeaseInfo.objects.bulk_update(
                    to_update, ['lease_hostname', 'active', 'lease_expiration', 'state_changed']
                )
        except Exception as exc:
            job_logger.warning(f'Scope {scope.name}: DHCPLeaseInfo bulk write failed: {exc}', exc_info=True)
            from django.db import connection
            connection.close()

    _pass3_done.append(True)

    # ------------------------------------------------------------------ #
    # Pass 4 — cleanup stale IPs                                          #
    # ------------------------------------------------------------------ #
    from netaddr import IPAddress as NetAddrIP

    # Range and exclusions are resolved once per scope, not per IP.
    try:
        _range = (NetAddrIP(scope.start_ip), NetAddrIP(scope.end_ip))
    except Exception:
        _range = None
    _exclusions = []
    for ex in scope.exclusion_ranges.all():
        try:
            _exclusions.append((NetAddrIP(ex.start_ip), NetAddrIP(ex.end_ip)))
        except Exception:
            continue

    def _in_dynamic_range(ip_str: str) -> bool:
        """Inside the scope's start/end range and outside every exclusion."""
        if _range is None:
            return False
        try:
            addr = NetAddrIP(ip_str)
        except Exception:
            return False
        if not _range[0] <= addr <= _range[1]:
            return False
        return not any(start <= addr <= end for start, end in _exclusions)

    def _downgrade(ip_obj, ip_str):
        # Pass 2 skipped this IP (reservation precedence), so apply the lease's
        # hostname/client_id now along with the status change.
        job_logger.debug(
            f'Downgraded IP {ip_str} {reservation_status!r}→{lease_status!r} '
            f'(reservation removed, lease still active)'
        )
        hostname, lease_client_id = lease_ips[ip_str]
        _update_ip(ip_obj, ip_str, lease_status, hostname, lease_client_id)

    for ip_str, ip_obj in list(existing.items()):
        tag_prot = _tag_protected(ip_obj)
        pfx_prot = _prefix_protected(ip_str)
        if tag_prot or pfx_prot:
            source = 'sync-protect tag' if tag_prot else 'protected prefix'
            job_logger.debug(f'Protected IP {ip_str}: skipped cleanup by {source}')
            continue

        if push_reservations and ip_obj.status == reservation_status:
            continue  # NetBox wins for reservations

        try:
            if _in_dynamic_range(ip_str):
                # Inside the dynamic range the server wins: only server-backed IPs survive.
                if ip_str in reservation_ips:
                    continue
                if ip_str in lease_ips:
                    if ip_obj.status == reservation_status:
                        _downgrade(ip_obj, ip_str)
                        _n_changed += 1
                    continue
                job_logger.debug(
                    f'Deleting IP {ip_str} (status {ip_obj.status!r}) — inside the scope range '
                    f'with no lease or reservation on the server'
                )
                ip_obj.delete()
                _n_changed += 1
                continue

            # Outside the range or inside an exclusion: only DHCP-managed IPs are cleaned up.
            if ip_obj.status not in (lease_status, reservation_status):
                continue
            if ip_obj.pk not in dhcp_managed_ids:
                continue  # hand-made — left alone

            if ip_obj.status == reservation_status:
                if ip_str in reservation_ips:
                    continue
                if ip_str in lease_ips:
                    _downgrade(ip_obj, ip_str)
                else:
                    job_logger.debug(f'Deleting IP {ip_str} — reservation and lease no longer exist on server')
                    ip_obj.delete()
                _n_changed += 1

            elif ip_str not in lease_ips:
                job_logger.debug(f'Deleting IP {ip_str} — lease expired or no longer exists on server')
                ip_obj.delete()
                _n_changed += 1

        except Exception as exc:
            job_logger.warning(f'Failed to clean up IP {ip_str}: {exc}', exc_info=True)
            from django.db import connection
            connection.close()

    if _late_client_changed:
        # A Pass 4 downgrade gave the IP a different client ID after Pass 3 had written.
        try:
            DHCPLeaseInfo.objects.filter(ip_address_id__in=_late_client_changed).update(
                state_changed=timezone.now()
            )
        except Exception as exc:
            job_logger.warning(f'Scope {scope.name}: Active/Inactive Since reset failed: {exc}', exc_info=True)
            from django.db import connection
            connection.close()

    return _n_processed, _n_changed


RESERVATION_TYPE = 'Both'

_DUPLICATE_CLIENT_ID_HINT = (
    ' A client ID must be unique within a scope, so two reservations can\'t swap '
    'client IDs in one step — give one of them a temporary client ID first.'
)


def _server_field(res, *keys):
    for key in keys:
        if key in res:
            return res.get(key)
    return None


def _order_updates(updates, server_by_ip):
    """
    Order client ID changes so a reservation giving up a client ID is updated before the
    one taking it. What's left in a cycle (a swap) keeps its order — PSU refuses it.
    """
    from .utils import normalize_client_id

    pending = list(updates)
    ordered = []
    while pending:
        held = {
            normalize_client_id(_server_field(server_by_ip[u['ip_address']], 'client_id', 'ClientId'))
            for u in pending
        }
        ready = [
            u for u in pending
            if 'client_id' not in u or normalize_client_id(u['client_id']) not in held
        ]
        if not ready:
            ordered.extend(pending)
            break
        ordered.extend(ready)
        pending = [u for u in pending if u not in ready]
    return ordered


def _log_reservation_results(job_logger, action, scope_id, results):
    """
    Log each per-item result of a bulk reservation call. Returns (ok, failed).
    A delete that finds nothing there is already in the wanted state — not a failure.
    """
    ok = failed = 0
    for r in results:
        ip = r.get('ip_address', '')
        status = r.get('status')
        if status == 'ok':
            ok += 1
            job_logger.info(f'Reservation {ip}: {action} on the server')
            continue
        if status == 'not_found':
            if action == 'deleted':
                job_logger.warning(f'Reservation {ip}: already gone from scope {scope_id} on the server')
                continue
            failed += 1
            job_logger.warning(
                f'Reservation {ip}: not {action} — no reservation at that IP in scope '
                f'{scope_id} on the server'
            )
            continue
        failed += 1
        error = r.get('error') or 'unknown error'
        if 'already used by' in error:
            error += _DUPLICATE_CLIENT_ID_HINT
        job_logger.warning(f'Reservation {ip}: not {action} — {error}')
    return ok, failed


PLACEHOLDER_CLIENT_ID_PREFIX = 'badc0ded'


def _new_placeholder_client_id(taken):
    """A placeholder client ID, 'ba-dc-0d-ed-' + 4 random bytes, not in `taken` (normalized)."""
    import secrets

    from .utils import format_client_id

    while True:
        value = PLACEHOLDER_CLIENT_ID_PREFIX + secrets.token_hex(4)
        if value not in taken:
            taken.add(value)
            return format_client_id(value)


def _assign_placeholders(job_logger, scope, ip_objs, server_by_ip, protect_tag='',
                         protected_prefix_networks=frozenset()):
    """
    Give each reservation-status IP with no client ID a placeholder client ID, so the
    server stops handing the IP out. Only IPs inside the scope's start/end range that
    aren't sync-protected (tag or prefix) qualify. The ID is unique within the scope,
    checked against NetBox and the server, and saved to the IP with a changelog entry.
    """
    import ipaddress as _ipmod

    from .utils import in_scope_range, normalize_client_id

    taken = {normalize_client_id(i.custom_field_data.get('dhcp_client_id')) for i in ip_objs}
    taken |= {
        normalize_client_id(_server_field(r, 'client_id', 'ClientId')) for r in server_by_ip.values()
    }
    for ip_obj in ip_objs:
        if ip_obj.custom_field_data.get('dhcp_client_id'):
            continue
        host = str(ip_obj.address.ip)
        if not in_scope_range(scope, host):
            continue
        if protect_tag and protect_tag in ip_obj.tags.slugs():
            continue
        if any(_ipmod.ip_address(host) in net for net in protected_prefix_networks):
            continue
        client_id = _new_placeholder_client_id(taken)
        ip_obj.snapshot()
        ip_obj.custom_field_data['dhcp_client_id'] = client_id
        ip_obj.save(update_fields=['custom_field_data', 'last_updated'])
        job_logger.info(f'Reservation {host}: no client ID — set placeholder {client_id}')


def _reconcile_reservations(job_logger, client, scope, scope_id: str, reservations,
                            reservation_status: str = 'reserved', by_ip: bool = True,
                            placeholders: bool = False, protect_tag: str = '',
                            protected_prefix_networks=frozenset()):
    """
    NetBox wins for reservations (push_reservations on): make the server's reservations
    in this scope match NetBox's reservation-status IPs in the scope prefix and VRF.

      - Wanted: every reservation-status IP with a client ID (inside or outside the
        range, in exclusions too). A client ID already wanted for another IP in the
        scope is skipped — Windows allows one reservation per client ID per scope.
      - Missing on the server → created, type Both.
      - Different on the server → updated by scope and IP. Compared: client ID
        (normalized), Name vs dns_name (ignoring case; left alone when the IP has the
        invalid-hostname tag and a blank dns_name), Description, and type.
      - On the server but not reservation-status in NetBox → deleted. A reservation-
        status IP with no client ID leaves the server's reservation alone.

    Deletes go first, then updates, then creates, each as bulk calls — so a client ID
    can move from one IP to another in one run. `reservations` is the server's list
    for this scope from the bulk fetch; server values are never written to NetBox.

    by_ip False (the server's PSU script predates the update/delete endpoints):
    create only, one call per reservation.

    placeholders True (the reservation_placeholders setting): reservation-status IPs
    with no client ID first get a placeholder one — see _assign_placeholders.

    Returns (changed, failed): whether any change was applied, and how many failed.
    A scope with no prefix has no NetBox IPs, so it's skipped — nothing is deleted.
    """
    from ipam.models import IPAddress

    from .utils import format_client_id, normalize_client_id

    if not scope.prefix_id:
        job_logger.info(f'Scope "{scope.name}": has no prefix — skipping its reservations')
        return False, 0

    server_by_ip = {}
    for r in reservations:
        ip = r.get('ip_address') or r.get('IPAddress')
        if ip:
            server_by_ip[ip] = r

    ip_objs = list(IPAddress.objects.filter(
        status=reservation_status,
        address__net_contained_or_equal=str(scope.prefix.prefix),
        vrf_id=scope.prefix.vrf_id,
    ).prefetch_related('tags'))
    if placeholders:
        _assign_placeholders(job_logger, scope, ip_objs, server_by_ip, protect_tag=protect_tag,
                             protected_prefix_networks=protected_prefix_networks)

    netbox_reserved = set()
    wanted = {}
    seen_client_ids = {}
    for ip_obj in ip_objs:
        host = str(ip_obj.address.ip)
        netbox_reserved.add(host)
        client_id = ip_obj.custom_field_data.get('dhcp_client_id') or ''
        if not client_id:
            job_logger.debug(
                f'Skipping reservation {host} — no dhcp_client_id set '
                f'(Windows DHCP requires a client ID to create a reservation)'
            )
            continue
        key = normalize_client_id(client_id)
        if key in seen_client_ids:
            job_logger.warning(
                f'Skipping reservation {host} — client_id {client_id} is already used by '
                f'{seen_client_ids[key]} in scope {scope_id} (duplicate client IDs not '
                f'allowed per scope)'
            )
            continue
        seen_client_ids[key] = host
        wanted[host] = ip_obj

    creates, updates, deletes = [], [], []
    for host, ip_obj in wanted.items():
        client_id = ip_obj.custom_field_data.get('dhcp_client_id')
        name = ip_obj.dns_name or ''
        description = ip_obj.description or ''
        remote = server_by_ip.get(host)
        if remote is None:
            creates.append({
                'scope_id': scope_id,
                'ip_address': host,
                'client_id': format_client_id(client_id),
                'name': name,
                'description': description,
                'type': RESERVATION_TYPE,
            })
            continue

        change = {}
        remote_client_id = _server_field(remote, 'client_id', 'ClientId')
        if normalize_client_id(remote_client_id) != normalize_client_id(client_id):
            change['client_id'] = format_client_id(client_id)
        invalid_blank = not name and any(t.slug == INVALID_HOSTNAME_TAG_SLUG for t in ip_obj.tags.all())
        remote_name = _server_field(remote, 'name', 'Name') or ''
        if not invalid_blank and remote_name.lower() != name.lower():
            change['name'] = name
        remote_description = _server_field(remote, 'description', 'Description')
        if remote_description is not None and remote_description != description:
            change['description'] = description
        remote_type = _server_field(remote, 'type', 'Type')
        if remote_type is not None and str(remote_type).lower() != RESERVATION_TYPE.lower():
            change['type'] = RESERVATION_TYPE
        if change:
            job_logger.debug(f'Reservation {host}: differs on the server — {sorted(change)}')
            updates.append({'scope_id': scope_id, 'ip_address': host, **change})

    for host in server_by_ip:
        if host not in netbox_reserved:
            deletes.append({'scope_id': scope_id, 'ip_address': host})

    if not by_ip:
        ok = failed = 0
        for item in creates:
            try:
                client.create_reservation(item)
                ok += 1
                job_logger.info(f'Reservation {item["ip_address"]}: created on the server')
            except Exception as exc:
                failed += 1
                job_logger.warning(f'Reservation {item["ip_address"]}: not created — {exc}')
        return ok > 0, failed

    ok = failed = 0
    for action, method, items in (
        ('deleted', client.delete_reservations, deletes),
        ('updated', client.update_reservations, _order_updates(updates, server_by_ip)),
        ('created', client.create_reservations, creates),
    ):
        if not items:
            continue
        _ok, _failed = _log_reservation_results(job_logger, action, scope_id, method(items))
        ok += _ok
        failed += _failed
    return ok > 0, failed


def _pull_exclusions(job_logger, scope, exclusions):
    """
    Reconcile NetBox exclusion ranges against a pre-fetched list from the DHCP server.
    Called when push_scope_info=False. Server is authoritative.
    - Server exclusions missing from NetBox are created.
    - NetBox exclusions no longer on the server are deleted.
    `exclusions` is the pre-extracted list for this scope from the bulk fetch.
    """
    from .models import DHCPExclusionRange

    scope_label = scope.name
    remote = {
        (r.get('start_ip') or r.get('StartRange'), r.get('end_ip') or r.get('EndRange'))
        for r in exclusions
        if (r.get('start_ip') or r.get('StartRange')) and (r.get('end_ip') or r.get('EndRange'))
    }
    local_qs = scope.exclusion_ranges.all()
    local = {(ex.start_ip, ex.end_ip): ex for ex in local_qs}

    _n_changed = 0
    for start, end in remote - set(local.keys()):
        DHCPExclusionRange.objects.create(scope=scope, start_ip=start, end_ip=end)
        _n_changed += 1
        job_logger.info(f'Scope "{scope_label}": added exclusion {start}–{end} from server')

    for (start, end), ex in local.items():
        if (start, end) not in remote:
            job_logger.info(f'Scope "{scope_label}": removed exclusion {start}–{end} — no longer on server')
            ex.delete()
            _n_changed += 1

    return len(remote), _n_changed


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


def _pull_options(job_logger, scope, options):
    """
    Reconcile NetBox option values against a pre-fetched list from the DHCP server.
    Called on every sync when push_scope_info=False. Server is authoritative.
    - Server options missing or changed in NetBox are added/replaced.
    - NetBox options no longer on the server are unlinked from the scope — the
      shared DHCPOptionValue row itself is never deleted, since other scopes may
      reference the same value (see DHCPOptionValue's docstring).
    Options 3 (Router) and 51 (Lease Time) are excluded — handled by _pull_scope_attributes.
    `options` is the pre-extracted list for this scope from the bulk fetch.
    """
    from .import_logic import get_or_create_option_value

    remote = _remote_option_map(options)
    local = _local_option_map(scope)

    _n_changed = 0
    for code, value in remote.items():
        existing = local.get(code)
        if existing is not None and existing.value == value:
            continue
        opt_val, _created = get_or_create_option_value(code, value)
        scope.option_values.add(opt_val)
        _n_changed += 1
        if existing is not None:
            scope.option_values.remove(existing)
            job_logger.info(f'Scope "{scope.name}": option {code} changed {existing.value!r} → {value!r} from server')
        else:
            job_logger.info(f'Scope "{scope.name}": option {code} added {value!r} from server')

    for code, ov in local.items():
        if code not in remote:
            scope.option_values.remove(ov)
            _n_changed += 1
            job_logger.info(f'Scope "{scope.name}": option {code} removed — no longer on server')

    return len(remote), _n_changed


def _remote_scope_description(remote):
    """
    The scope description from a PSU scope record, trimmed to DHCPScope.description's
    length — or None when the record has no description key at all, so a response
    without one never reads as "blank" and wipes the NetBox value.
    """
    for key in ('description', 'Description'):
        if key in remote:
            return (remote.get(key) or '')[:200]
    return None


def _pull_scope_attributes(job_logger, scope, remote):
    """
    Update NetBox scope fields to match the live DHCP server (server is authoritative).
    Called on every sync when push_scope_info=False.
    Logs and saves only fields that actually differ. The router is compared only
    when `remote` carries it (see remote_has_router).
    """
    from .import_logic import remote_has_router

    scope_label = scope.name  # capture before any name change

    remote_name   = remote.get('name')     or remote.get('Name')       or ''
    remote_start  = remote.get('start_ip') or remote.get('StartRange') or ''
    remote_end    = remote.get('end_ip')   or remote.get('EndRange')   or ''
    router_known  = remote_has_router(remote)
    router_raw    = remote.get('router')   or remote.get('Router')     or ''
    remote_router = router_raw if router_raw not in ('', '0.0.0.0') else None
    remote_lease  = int(remote.get('lease_duration_seconds') or remote.get('LeaseDuration') or 86400)
    remote_desc   = _remote_scope_description(remote)
    remote_active = _remote_scope_active(remote)

    # Collect (field_name, old_value, new_value) tuples for fields that need updating.
    changes = []
    if remote_name and scope.name != remote_name:
        changes.append(('name', scope.name, remote_name))
    if remote_desc is not None and scope.description != remote_desc:
        changes.append(('description', scope.description, remote_desc))
    if remote_start and scope.start_ip != remote_start:
        changes.append(('start_ip', scope.start_ip, remote_start))
    if remote_end and scope.end_ip != remote_end:
        changes.append(('end_ip', scope.end_ip, remote_end))
    if router_known and scope.router != remote_router:
        changes.append(('router', scope.router, remote_router))
    if scope.lease_lifetime != remote_lease:
        changes.append(('lease_lifetime', scope.lease_lifetime, remote_lease))
    if scope.active != remote_active:
        changes.append(('active', scope.active, remote_active))

    if not changes:
        job_logger.debug(f'Scope "{scope_label}": all attributes match server')
        return False

    scope.snapshot()
    for field, old_val, new_val in changes:
        setattr(scope, field, new_val)
        job_logger.info(f'Scope "{scope_label}": {field} updated {old_val!r} → {new_val!r} from server')
    scope.save(update_fields=[f for f, _, _ in changes] + ['last_updated'])
    return True


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
    from .utils import AmbiguousFailover, find_failover

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
        scope.save(update_fields=['failover', 'server', 'last_updated'])
        job_logger.info(f'Scope "{scope.name}": failover cleared (was {current_name!r}) — server reports standalone')
        return

    try:
        failover = find_failover(remote_failover_name, server)
    except AmbiguousFailover as exc:
        job_logger.warning(f'Scope "{scope.name}": {exc} — failover not updated')
        return
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
    scope.save(update_fields=['failover', 'server', 'last_updated'])
    job_logger.info(f'Scope "{scope.name}": failover updated {current_name!r} → {remote_failover_name!r} from server')


def _push_scope(job_logger, client, scope, remote=None, scope_id: Optional[str] = None,
                push_state: bool = False):
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

    The scope's active state is sent (as `state`) and compared only when `push_state`
    — the server's PSU script accepts it (psu_supports_scope_state). On a failover
    scope it reaches the partner through replication, like every other setting.

    Returns True if a create/update was actually sent to the server (used by
    the caller to decide whether this scope needs failover replication),
    False if nothing needed pushing or the push failed.
    """
    from .api_client import PSUClientError
    from .import_logic import denormalize_option_value, remote_has_router, router_from_options

    local_failover_name = scope.failover.name if scope.failover_id else None
    failover_attempted = False

    try:
        scope_net = scope.network_cidr
        if scope_net is None:
            raise ValueError(f'no valid network stored ({scope.network}/{scope.prefix_length})')
        local_options = {code: ov.value for code, ov in _local_option_map(scope).items()}
        payload = {
            'scope_id': scope_id or str(scope_net.network),
            'name': scope.name,
            'start_ip': scope.start_ip,
            'end_ip': scope.end_ip,
            'subnet_mask': str(scope_net.netmask),
            'router': scope.router or '',
            'lease_duration_seconds': scope.lease_lifetime,
            'description': scope.description or '',
        }
        if push_state:
            payload['state'] = 'Active' if scope.active else 'InActive'

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
            if not push_state and not scope.active:
                job_logger.warning(
                    f'Scope {payload["scope_id"]}: inactive in NetBox, but the PSU script can\'t '
                    f'set a scope\'s state — it was created active on the server'
                )
            return True

        # Compare with remote to avoid pushing when nothing has changed.
        if remote is not None:
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
            if remote_lease != scope.lease_lifetime:
                diffs.append('lease_lifetime')
            remote_desc = _remote_scope_description(remote)
            if remote_desc is not None and remote_desc != (scope.description or ''):
                diffs.append('description')
            if push_state and _remote_scope_active(remote) != scope.active:
                diffs.append('active')

            options_set = []
            options_remove = []
            try:
                remote_options_raw = client.list_scope_options(scope_id)
                remote_options = _remote_option_map(remote_options_raw)
            except PSUClientError as exc:
                job_logger.warning(f'Scope {scope_id}: could not fetch remote options — skipping option push: {exc}')
                remote_options_raw = remote_options = None

            # The router: from the scope list (older PSU scripts), else from the scope's
            # options (Option 3). Without either, it isn't compared this run.
            if remote_has_router(remote):
                router_raw = remote.get('router') or remote.get('Router') or ''
                remote_router = router_raw if router_raw not in ('', '0.0.0.0') else None
                router_known = True
            elif remote_options_raw is not None:
                remote_router = router_from_options(remote_options_raw)
                router_known = True
            else:
                router_known = False
            if router_known and remote_router != scope.router:
                diffs.append('router')

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


def _remote_scope_active(remote) -> bool:
    """True unless the server reports the scope as inactive (a missing state counts as active)."""
    state = remote.get('state') or remote.get('State')
    return state is None or str(state).strip().lower() == 'active'


def _remote_scope_map(remote_scopes):
    """{scope_id: remote} for every scope the server returned, active or not."""
    remote_map = {}
    for rs in remote_scopes:
        scope_id = rs.get('scope_id') or rs.get('ScopeId') or rs.get('network_address')
        if scope_id:
            remote_map[scope_id] = rs
    return remote_map


def _remote_failover_name(remote):
    return (
        remote.get('failover_name') or remote.get('FailoverName')
        or remote.get('FailoverRelationshipName') or None
    )


def _sync_server(job_logger, server, sync_ip_addresses: bool, push_reservations: bool,
                 push_scope_info: bool,
                 protect_tag: str = '', update_client_id: bool = False,
                 lease_status: str = 'dhcp', reservation_status: str = 'reserved',
                 fallback_failover_ids=None, protected_prefix_networks=frozenset(),
                 reservation_placeholders: bool = False):
    """
    fallback_failover_ids: set of DHCPFailover PKs whose primary is down — this server
    (the secondary) should handle those failover scopes in place of the primary.
    """
    from django.utils import timezone

    from .api_client import PSUClient, PSUClientError
    from .import_logic import remote_has_router, router_from_options
    from .models import DHCPFailover, DHCPScope, DHCPServer
    from .utils import (
        AmbiguousFailover, find_failover, psu_supports_reservation_by_ip,
        psu_supports_scope_state, scopes_for_server,
    )

    if server.maintenance_mode:
        job_logger.info(f'Skipping server {server.name}: maintenance mode enabled')
        return

    if server.access_level == DHCPServer.ACCESS_RO and (push_reservations or push_scope_info):
        job_logger.info(
            f'Server {server.name}: API token is read-only — pulling only, '
            f'skipping scope/reservation push this run'
        )
        push_reservations = False
        push_scope_info = False

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

    _t_psu_bulk_start = time.perf_counter()
    _t_ip_updates = 0.0
    _t_pull_scope_attrs = 0.0
    _t_pull_exclusions = 0.0
    _t_pull_options = 0.0
    _n_scopes_synced = 0
    _n_ip_processed = 0
    _n_ip_changed = 0
    _n_excl_processed = 0
    _n_excl_changed = 0
    _n_opts_processed = 0
    _n_opts_changed = 0
    _n_attrs_processed = 0
    _n_attrs_changed = 0
    _n_res_failed = 0

    by_ip = psu_supports_reservation_by_ip(server)
    if push_reservations and not by_ip:
        job_logger.warning(
            f'Server {server.name}: PSU script v{server.psu_script_version or "unknown"} can\'t '
            f'update or delete reservations — only creating missing ones. Run "Update PSU '
            f'Scripts" to enable the full reservation push.'
        )

    # Older PSU scripts ignore a scope's `state`: don't push it to them, or it would count
    # as a difference (and be pushed again) on every sync.
    push_state = psu_supports_scope_state(server)
    if push_scope_info and not push_state:
        job_logger.info(
            f'Server {server.name}: PSU script v{server.psu_script_version or "unknown"} can\'t '
            f'set a scope\'s active state — not pushing it. Run "Update PSU Scripts" to enable it.'
        )

    # Every scope, active or not.
    try:
        remote_scopes = client.list_scopes(include_router=False)
    except PSUClientError as exc:
        job_logger.error(f'Failed to fetch scopes from {server.name}: {exc}')
        return

    # Bulk-fetch leases/reservations/exclusions/options for every scope on this server
    # in one call each, instead of one call per scope in the loop below — PSU pays a
    # flat ~85-125ms per-request overhead regardless of payload size, so a handful of
    # bulk calls is far faster than hundreds of small ones at scale.
    # Leases and reservations are an atomic pair for the sync_ip_addresses/cleanup path:
    # if either fetch fails, both maps are set to None (never empty dict) so a failure
    # is never misread as "confirmed nothing exists" and never drives _cleanup_stale_ips
    # to delete anything. push_reservations only needs reservations, so a leases-only
    # failure doesn't block it.
    # Exclusions and options use the same None-on-failure pattern for the same reason.
    leases_by_scope = None
    reservations_by_scope = None
    exclusions_by_scope = None
    options_by_scope = None

    if sync_ip_addresses:
        try:
            leases_by_scope = client.list_leases()  # returns dict (format=grouped)
        except PSUClientError as exc:
            job_logger.error(f'Failed to bulk-fetch leases from {server.name}: {exc} — skipping IP cleanup this run')

    if sync_ip_addresses or push_reservations:
        try:
            reservations_by_scope = client.list_reservations()  # returns dict (format=grouped)
        except PSUClientError as exc:
            job_logger.error(
                f'Failed to bulk-fetch reservations from {server.name}: {exc} — '
                f'skipping IP cleanup and reservation pushes this run'
            )

    if not push_scope_info:
        try:
            exclusions_by_scope = client.list_all_exclusions()
        except PSUClientError as exc:
            job_logger.error(
                f'Failed to bulk-fetch exclusions from {server.name}: {exc} — '
                f'skipping exclusion reconciliation this run'
            )
        try:
            options_by_scope = client.list_all_scope_options()
        except PSUClientError as exc:
            job_logger.error(
                f'Failed to bulk-fetch options from {server.name}: {exc} — '
                f'skipping option reconciliation this run'
            )

    _t_psu_bulk = time.perf_counter() - _t_psu_bulk_start

    # Cleanup needs leases and reservations together — seeing only one list could make a
    # still-valid lease or reservation look stale — so it's gated on both being present,
    # not just leases_by_scope, even though only the leases fetch could fail here.
    cleanup_data_available = leases_by_scope is not None and reservations_by_scope is not None

    # Maps: network_address -> remote scope dict. Inactive scopes are matched like any
    # other: their active state is just another scope setting.
    remote_scope_map = _remote_scope_map(remote_scopes)

    job_logger.info(
        f'Server {server.name} returned {len(remote_scope_map)} remote scope(s): '
        f'{list(remote_scope_map.keys())}'
    )

    # Local lookup: network address → DHCPScope, for the scopes tied to this server only
    # (its standalone scopes, and the scopes of failovers it's a partner in) — so another
    # server's scope with the same network is never matched. Two of this server's scopes
    # with the same network can't be told apart: both are skipped, and so is the server
    # scope with that network (never guess).
    local_scope_map = {}
    duplicate_networks = {}
    for scope in scopes_for_server(server).select_related('prefix', 'failover').prefetch_related(
        'option_values__option_definition'
    ):
        network = scope.network
        if not network:
            job_logger.warning(f'Scope "{scope.name}": no network stored — skipping')
            continue
        if network in duplicate_networks:
            duplicate_networks[network].append(scope.name)
        elif network in local_scope_map:
            duplicate_networks[network] = [local_scope_map.pop(network).name, scope.name]
        else:
            local_scope_map[network] = scope
    for network, names in sorted(duplicate_networks.items()):
        job_logger.warning(
            f'Server {server.name}: NetBox scopes {sorted(names)} all use network {network} — '
            f'skipping them and the server scope {network} until only one is left'
        )

    # Scope IDs (this server's) that changed during this run and belong to a
    # failover relationship — replicated in one batched call at the end, rather
    # than per-scope, since Invoke-DhcpServerv4FailoverReplication is expensive.
    failover_replicate_scope_ids = set()

    # Iterate remote scopes only — these are the scopes that belong to this server.
    # Looking up from the remote side means we never touch scopes from other servers.
    for scope_id, remote in remote_scope_map.items():
        if scope_id in duplicate_networks:
            continue
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
                    try:
                        failover = find_failover(remote_failover_name, server)
                    except AmbiguousFailover as exc:
                        job_logger.warning(f'Remote scope {scope_id}: {exc} — skipping cleanup.')
                        continue
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
                        job_logger.info(
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
            # participates in normal sync from this point on. The same guards as the
            # cleanup branch above come first, except that the secondary may import a
            # failover scope while it stands in for a down primary.
            remote_failover_name = _remote_failover_name(remote)
            if remote_failover_name:
                try:
                    failover = find_failover(remote_failover_name, server)
                except AmbiguousFailover as exc:
                    job_logger.warning(f'Remote scope {scope_id}: {exc} — not importing it.')
                    continue
                if failover is None:
                    job_logger.error(
                        f'Remote scope {scope_id} reports failover {remote_failover_name!r}, which '
                        f'does not exist in NetBox — not importing it. Re-run "Import from Server" '
                        f'for {server.name} to create the failover.'
                    )
                    continue
                if failover.maintenance_mode:
                    job_logger.info(
                        f'Remote scope {scope_id}: failover {remote_failover_name!r} is in '
                        f'maintenance mode — not importing it'
                    )
                    continue
                if not failover.sync_enabled:
                    job_logger.info(
                        f'Remote scope {scope_id}: failover {remote_failover_name!r} has sync '
                        f'disabled — not importing it'
                    )
                    continue
                if failover.primary_server_id != server.pk and failover.pk not in fallback_failover_ids:
                    job_logger.debug(
                        f'Remote scope {scope_id} belongs to failover {remote_failover_name!r} — '
                        f'only the primary ({failover.primary_server.name}) imports it.'
                    )
                    continue
            elif not server.sync_standalone_scopes:
                job_logger.debug(
                    f'Remote scope {scope_id}: standalone scopes disabled on {server.name} — '
                    f'not importing it'
                )
                continue
            from .import_logic import _import_scope
            _import_results = {
                'scopes':           {'created': [], 'skipped': [], 'errors': []},
                'option_values':    {'created': [], 'skipped': [], 'errors': []},
                'exclusion_ranges': {'created': [], 'skipped': [], 'errors': []},
            }
            scope = _import_scope(client, remote, _import_results, server=server)
            for created in _import_results['scopes']['created']:
                job_logger.info(f'Auto-created scope from {server.name}: {created}')
            for unassigned in _import_results['scopes'].get('unassigned', []):
                job_logger.warning(f'Auto-created scope from {server.name}: {unassigned}')
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
                job_logger.info(
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

        job_logger.debug(f'Scope "{scope.name}" matched remote scope_id={scope_id} on {server.name}')
        _n_scopes_synced += 1

        # Scope first: with push_scope_info off, the range and exclusions pulled here are the
        # ones the IP cleanup below uses, so a range change on the server applies this run.
        _t0 = time.perf_counter()
        if push_scope_info:
            scope_pushed = _push_scope(job_logger, client, scope, remote=remote, scope_id=scope_id,
                                       push_state=push_state)
            exclusions_changed = _sync_exclusions(job_logger, client, scope, scope_id)
            if scope.failover_id and (scope_pushed or exclusions_changed):
                failover_replicate_scope_ids.add(scope_id)
        else:
            # Newer PSU scripts leave the router out of the scope list: take it from the
            # scope's options (Option 3). Without options data for the scope, the router
            # is left as it is this run.
            if (not remote_has_router(remote) and options_by_scope is not None
                    and scope_id in options_by_scope):
                remote = {**remote, 'router': router_from_options(options_by_scope[scope_id]) or ''}
            attrs_changed = _pull_scope_attributes(job_logger, scope, remote)
            _pull_scope_failover(job_logger, scope, remote, server)
            _n_attrs_processed += 1
            if attrs_changed:
                _n_attrs_changed += 1
            _t_pull_scope_attrs += time.perf_counter() - _t0
            _t0 = time.perf_counter()
            if exclusions_by_scope is not None:
                _excl_proc, _excl_chg = _pull_exclusions(job_logger, scope, exclusions_by_scope.get(scope_id, []))
                _n_excl_processed += _excl_proc
                _n_excl_changed += _excl_chg
            _t_pull_exclusions += time.perf_counter() - _t0
            _t0 = time.perf_counter()
            if options_by_scope is not None:
                _opts_proc, _opts_chg = _pull_options(job_logger, scope, options_by_scope.get(scope_id, []))
                _n_opts_processed += _opts_proc
                _n_opts_changed += _opts_chg
            _t_pull_options += time.perf_counter() - _t0


        if not scope.prefix_id:
            job_logger.info(
                f'Scope "{scope.name}" ({scope_id}): has no prefix — skipping IP sync and reservations'
            )
            DHCPScope.objects.filter(pk=scope.pk).update(last_sync_at=timezone.now())
            continue

        _t0 = time.perf_counter()

        if sync_ip_addresses:
            if cleanup_data_available:
                leases = leases_by_scope.get(scope_id, [])
                reservations = reservations_by_scope.get(scope_id, [])
                job_logger.info(f'Scope {scope_id}: {len(leases)} lease(s), {len(reservations)} reservation(s)')
                _ip_proc, _ip_chg = _sync_scope_ips(
                    job_logger, scope, leases, reservations,
                    protect_tag=protect_tag, update_client_id=update_client_id,
                    lease_status=lease_status, reservation_status=reservation_status,
                    protected_prefix_networks=protected_prefix_networks,
                    push_reservations=push_reservations,
                )
                _n_ip_processed += _ip_proc
                _n_ip_changed += _ip_chg
        else:
            job_logger.info(f'Scope {scope_id}: skipping IP updates (sync_ip_addresses=False)')
        _t_ip_updates += time.perf_counter() - _t0

        if push_reservations:
            if reservations_by_scope is not None:
                res_changed, res_failed = _reconcile_reservations(
                    job_logger, client, scope, scope_id,
                    reservations_by_scope.get(scope_id, []),
                    reservation_status=reservation_status, by_ip=by_ip,
                    placeholders=reservation_placeholders, protect_tag=protect_tag,
                    protected_prefix_networks=protected_prefix_networks,
                )
                _n_res_failed += res_failed
                if scope.failover_id and res_changed:
                    failover_replicate_scope_ids.add(scope_id)
            else:
                job_logger.error(f'Scope {scope_id}: skipping reservation push — reservations fetch failed this run')

        DHCPScope.objects.filter(pk=scope.pk).update(last_sync_at=timezone.now())

    # Handle local scopes that have no matching remote scope on this server — the
    # same guards as the remote-orphan cleanup above. Failover scopes are only ever
    # handled from their primary's own sync: never the secondary, and never while the
    # secondary stands in for a down primary (it could be missing a scope the primary
    # has, and must never be pushed to directly).
    for network, scope in local_scope_map.items():
        if network in remote_scope_map:
            continue  # already handled in the loop above

        if scope.failover_id:
            if scope.failover.primary_server_id != server.pk:
                continue
            if scope.failover.maintenance_mode:
                job_logger.info(
                    f'Scope "{scope.name}": failover "{scope.failover.name}" is in maintenance mode — '
                    f'skipping missing-scope handling'
                )
                continue
            if not scope.failover.sync_enabled:
                job_logger.info(
                    f'Scope "{scope.name}": failover "{scope.failover.name}" has sync disabled — '
                    f'skipping missing-scope handling'
                )
                continue
        elif scope.server_id == server.pk:
            if not server.sync_standalone_scopes:
                job_logger.debug(
                    f'Scope "{scope.name}": standalone scopes disabled on {server.name} — '
                    f'skipping missing-scope handling'
                )
                continue
        else:
            continue

        if scope.maintenance_mode:
            job_logger.info(f'Scope "{scope.name}": in maintenance mode — skipping missing-scope handling')
            continue

        if push_scope_info:
            # Push scope info is on — create the missing scope on the server. Its
            # reservations follow in the same run (a new scope has none on the server).
            scope_pushed = _push_scope(job_logger, client, scope, push_state=push_state)
            if scope_pushed and push_reservations:
                res_changed, res_failed = _reconcile_reservations(
                    job_logger, client, scope, network, [],
                    reservation_status=reservation_status, by_ip=by_ip,
                    placeholders=reservation_placeholders, protect_tag=protect_tag,
                    protected_prefix_networks=protected_prefix_networks,
                )
                _n_res_failed += res_failed
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
                from django.db import connection
                connection.close()  # reset a connection possibly corrupted by a mid-query job timeout

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

    return {
        'server': server.name,
        'scopes_fetched': len(remote_scope_map),
        'scopes_synced': _n_scopes_synced,
        'psu_bulk_s': round(_t_psu_bulk, 3),
        'ip_s': round(_t_ip_updates, 3),
        'ip_processed': _n_ip_processed,
        'ip_changed': _n_ip_changed,
        'excl_s': round(_t_pull_exclusions, 3),
        'excl_processed': _n_excl_processed,
        'excl_changed': _n_excl_changed,
        'opts_s': round(_t_pull_options, 3),
        'opts_processed': _n_opts_processed,
        'opts_changed': _n_opts_changed,
        'attrs_s': round(_t_pull_scope_attrs, 3),
        'attrs_processed': _n_attrs_processed,
        'attrs_changed': _n_attrs_changed,
        'reservation_failures': _n_res_failed,
    }


def _reservation_failure_summary(server_name, failed):
    return f'{server_name}: {failed} reservation change(s) failed — see log'


def _fail_job(job, error):
    """
    End the job as Failed (not Errored — an expected, handled outcome) with `error`
    shown on the job page. A recurring job still reschedules.
    """
    from core.exceptions import JobFailed

    job.error = error
    job.save(update_fields=['error'])
    raise JobFailed()


# ---------------------------------------------------------------------------
# Job classes
# ---------------------------------------------------------------------------

class DHCPSyncJob(JobRunner):
    """
    Synchronise all DHCP servers with NetBox.

    Not a @system_job: the recurring chain is started only by a human (via
    SetDHCPSyncScheduleJob's "Schedule" action) and self-perpetuates from
    there — JobRunner.handle()'s finally block reschedules any job with a
    truthy interval. A @system_job would auto-create a schedule on every
    worker restart even when none exists.
    """

    class Meta:
        name = 'Windows DHCP Sync'
        description = (
            'Pulls scope, lease, and reservation data from Windows DHCP servers '
            'via PowerShell Universal and updates NetBox IP Address objects.'
        )

    def __init__(self, job):
        super().__init__(job)
        _quiet_file_log(self.logger)
        _apply_sync_log_level(self.logger)

    @classmethod
    def enqueue(cls, *args, **kwargs):
        """
        Force every enqueue — the scheduler job and JobRunner.handle()'s
        auto-reschedule alike — to run as DHCP-Sync-Service and to pull
        queue/timeout/interval from live plugin settings, so no caller needs
        to pass them.

        interval is overwritten rather than defaulted, unless the caller
        explicitly passed interval=None (a one-off run that must not
        auto-reschedule): handle()'s auto-reschedule always passes the prior
        job's own interval explicitly, so a plain setdefault() would never
        see a live change.
        """
        service_user = _service_user()
        if service_user is not None:
            kwargs['user'] = service_user
        try:
            cfg = _load_settings()
            kwargs.setdefault('queue_name', cfg.sync_queue)
            kwargs.setdefault('job_timeout', cfg.sync_job_timeout)
            if not ('interval' in kwargs and kwargs['interval'] is None):
                kwargs['interval'] = cfg.sync_interval
        except Exception:
            # Settings may not be loadable during initial migrations — fall through
            # with whatever the caller provided.
            pass
        return super().enqueue(*args, **kwargs)

    @classmethod
    def converge_schedule(cls, **enqueue_once_kwargs):
        """
        Collapse any duplicate pending/scheduled jobs of this name down to
        one, then optionally hand off to enqueue_once() to apply new kwargs
        (e.g. an updated interval) to the survivor.

        enqueue_once() alone can't do this — it only inspects the single
        most-recently-created row, so a second duplicate chain is invisible
        to it. Deletes per-instance (not via queryset) since Job.delete() is
        overridden to also cancel the Redis-side entry, which a bulk delete
        would skip.
        """
        from core.choices import JobStatusChoices
        from core.models import Job
        from django.db.models import F
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

        # Converge any duplicate recurring chains down to this job on every
        # recurring run — see converge_schedule() above for why enqueue_once()
        # alone can't do this. Gated on self.job.interval so a one-off run
        # (interval=None, e.g. "Run Now") never touches the recurring chain's
        # own scheduled entry.
        if self.job.interval:
            from core.choices import JobStatusChoices
            from core.models import Job
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
            client = PSUClient(server)
            try:
                result = client.ping_read()
            except PSUClientError as exc:
                server.health_status = DHCPServer.HEALTH_UNREACHABLE
                server.access_level = DHCPServer.ACCESS_UNKNOWN
                server.last_health_check = now
                server.health_error = str(exc)
                server.save(update_fields=[
                    'health_status', 'access_level', 'last_health_check', 'health_error'
                ])
                self.logger.warning(f'Server {server.name}: unreachable — {exc}')
                run_errors.append(f'{server.name} unreachable: {exc}')
                continue

            version = result.get('version', '')
            server.health_status = DHCPServer.HEALTH_HEALTHY
            server.last_health_check = now
            server.health_error = ''
            server.psu_script_version = version

            try:
                client.ping_write()
                server.access_level = DHCPServer.ACCESS_RW
            except PSUClientError as write_exc:
                if write_exc.status_code == 403:
                    server.access_level = DHCPServer.ACCESS_RO
                else:
                    server.health_status = DHCPServer.HEALTH_UNREACHABLE
                    server.access_level = DHCPServer.ACCESS_UNKNOWN
                    server.health_error = str(write_exc)
                    server.save(update_fields=[
                        'health_status', 'access_level', 'last_health_check',
                        'health_error', 'psu_script_version',
                    ])
                    self.logger.warning(f'Server {server.name}: unreachable — {write_exc}')
                    run_errors.append(f'{server.name} unreachable: {write_exc}')
                    continue

            server.save(update_fields=[
                'health_status', 'access_level', 'last_health_check', 'health_error', 'psu_script_version'
            ])
            if version and version != PSU_SCRIPT_VERSION:
                self.logger.warning(
                    f'Server {server.name}: PSU script version mismatch '
                    f'(expected {PSU_SCRIPT_VERSION}, got {version})'
                )
            else:
                self.logger.info(f'Server {server.name}: healthy (PSU script v{version or "unknown"})')

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

        timing_results = []

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
                    _timing = _sync_server(
                        self.logger, server, sync_ip_addresses, push_reservations, push_scope_info,
                        protect_tag=protect_tag, update_client_id=update_client_id,
                        lease_status=lease_status, reservation_status=reservation_status,
                        fallback_failover_ids=server_fallbacks[server.pk],
                        protected_prefix_networks=protected_prefix_networks,
                        reservation_placeholders=cfg.reservation_placeholders,
                    )
                    if _timing:
                        _res_failed = _timing.pop('reservation_failures', 0)
                        if _res_failed:
                            run_errors.append(_reservation_failure_summary(server.name, _res_failed))
                        timing_results.append(_timing)
                except Exception as exc:
                    self.logger.error(f'Error syncing server {server.name}: {exc}')
                    from django.db import connection
                    connection.close()  # reset a connection possibly corrupted by a mid-query job timeout
                    try:
                        DHCPServer.objects.filter(pk=server.pk).update(last_sync_error=str(exc))
                    except Exception as db_exc:
                        self.logger.error(
                            f'Could not record sync error for {server.name} '
                            f'({type(db_exc).__name__}): {db_exc}'
                        )
                        connection.close()  # reset again in case this second write also hit corruption
                    run_errors.append(f'{server.name} sync error: {exc}')

        for t in timing_results:
            self.logger.info(f'[TIMING] {json.dumps(t)}')

        if run_errors:
            from core.exceptions import JobFailed
            self.job.error = '; '.join(run_errors)
            self.job.save(update_fields=['error'])
            raise JobFailed()


class SetDHCPSyncScheduleJob(JobRunner):
    """
    One-shot job that enqueues or reschedules "Windows DHCP Sync" on behalf
    of whoever requested it.

    Runs as the requesting user (view passes user=request.user), so the Jobs
    list shows who asked for it — DHCPSyncJob itself always runs as
    DHCP-Sync-Service instead (see DHCPSyncJob.enqueue()).

    kwargs:
        recurring: True to (re)schedule the recurring chain ("Schedule"),
            collapsing any duplicate chains via converge_schedule(). False
            for a one-off run alongside the existing chain ("Run Now"),
            which never disturbs it.
        sync_at: When the recurring chain's next run should start. Only used
            when recurring=True.
    """

    class Meta:
        name = 'Set DHCP Sync Schedule'
        description = 'Schedules, or immediately triggers, the recurring Windows DHCP Sync job.'

    def run(self, *args, **kwargs):
        if kwargs.get('recurring'):
            sync_at = kwargs.get('sync_at')
            cfg = _load_settings()
            job = DHCPSyncJob.converge_schedule(schedule_at=sync_at, interval=cfg.sync_interval)
            self.logger.info(
                f'Scheduled recurring "{DHCPSyncJob.name}" to start {sync_at} '
                f'(job pk={job.pk}), then every {cfg.sync_interval} minute(s).'
            )
        else:
            job = DHCPSyncJob.enqueue(interval=None)
            self.logger.info(f'Enqueued one-off "{DHCPSyncJob.name}" run (job pk={job.pk}).')


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

    from core.exceptions import JobFailed

    from .api_client import PSUClient, PSUClientError
    from .constants import PSU_SCRIPT_VERSION
    from .models import DHCPServer

    now = timezone.now()

    def _unreachable(exc):
        server.health_status = DHCPServer.HEALTH_UNREACHABLE
        server.access_level = DHCPServer.ACCESS_UNKNOWN
        server.last_health_check = now
        server.health_error = str(exc)
        server.save(update_fields=[
            'health_status', 'access_level', 'last_health_check', 'health_error'
        ])
        logger.error(f'Server unreachable — {exc}')
        job.error = f'{server.name} unreachable: {exc}'
        job.save(update_fields=['error'])
        raise JobFailed()

    client = PSUClient(server)
    try:
        result = client.ping_read()
    except PSUClientError as exc:
        _unreachable(exc)

    version = result.get('version', '')
    server.health_status = DHCPServer.HEALTH_HEALTHY
    server.last_health_check = now
    server.health_error = ''
    server.psu_script_version = version

    try:
        client.ping_write()
        server.access_level = DHCPServer.ACCESS_RW
    except PSUClientError as write_exc:
        if write_exc.status_code == 403:
            server.access_level = DHCPServer.ACCESS_RO
        else:
            _unreachable(write_exc)

    server.save(update_fields=[
        'health_status', 'access_level', 'last_health_check', 'health_error', 'psu_script_version'
    ])
    if version and version != PSU_SCRIPT_VERSION:
        logger.warning(
            f'PSU script version mismatch '
            f'(expected {PSU_SCRIPT_VERSION}, got {version})'
        )
    else:
        logger.info(f'Health check passed (PSU script v{version or "unknown"})')


class DHCPServerSyncJob(JobRunner):
    """Sync a single DHCPServer on demand (enqueued by the Sync Now button)."""

    class Meta:
        name = 'Windows DHCP Server Sync'
        description = 'On-demand sync for a single Windows DHCP server.'

    def __init__(self, job):
        super().__init__(job)
        _quiet_file_log(self.logger)
        _apply_sync_log_level(self.logger)

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
            _timing = _sync_server(
                self.logger, server,
                cfg.sync_ip_addresses, cfg.push_reservations, cfg.push_scope_info,
                protect_tag=protect_tag,
                update_client_id=cfg.sync_protect_update_client_id,
                lease_status=cfg.lease_status,
                reservation_status=cfg.reservation_status,
                protected_prefix_networks=protected_prefix_networks,
                reservation_placeholders=cfg.reservation_placeholders,
            )
        if _timing:
            _res_failed = _timing.pop('reservation_failures', 0)
            self.logger.info(f'[TIMING] {json.dumps(_timing)}')
            if _res_failed:
                _fail_job(self.job, _reservation_failure_summary(server.name, _res_failed))


def _push_skip_reason(scope, server):
    """
    Why `scope` must not be pushed to `server` right now, or None if it may be. Shared by
    the immediate scope and reservation pushes. A failover scope is only ever pushed to
    the failover's primary — Windows failover replication carries it to the secondary.
    Returns '' for a skip not worth logging above debug (another server's scope).
    """
    if scope.maintenance_mode:
        return f'Scope "{scope.name}": in maintenance mode — skipping'
    if scope.failover_id:
        if scope.failover.maintenance_mode:
            return f'Scope "{scope.name}": failover "{scope.failover.name}" is in maintenance mode — skipping'
        if not scope.failover.sync_enabled:
            return f'Scope "{scope.name}": failover "{scope.failover.name}" has sync disabled — skipping'
        if server.pk != scope.failover.primary_server_id:
            return ''
        return None
    if scope.server_id:
        if scope.server_id != server.pk:
            return ''
        if not server.sync_standalone_scopes:
            return f'Scope "{scope.name}": standalone scopes disabled on {server.name} — skipping'
        return None
    return f'Scope "{scope.name}": no server or failover assigned — skipping'


def _protected_prefix_networks(cfg):
    """The networks of every prefix carrying the sync-protect tag."""
    import ipaddress as _ipmod
    from ipam.models import Prefix

    networks = set()
    if cfg.sync_protect_tag_id:
        for p in Prefix.objects.filter(tags__slug=cfg.sync_protect_tag.slug):
            try:
                networks.add(_ipmod.ip_network(str(p.prefix), strict=False))
            except ValueError:
                pass
    return networks


def _fetch_scope_reservations(job_logger, client, scope_ids):
    """
    The server's reservations for `scope_ids`, as {scope_id: [...]}: one call for one
    scope, one bulk call otherwise. A scope that isn't on the server, or whose fetch
    failed, is left out — the caller skips it and deletes nothing.
    """
    from .api_client import PSUClientError

    scope_ids = list(scope_ids)
    if not scope_ids:
        return {}
    try:
        if len(scope_ids) == 1:
            return {scope_ids[0]: client.list_reservations(scope_ids[0])}
        grouped = client.list_reservations()
    except PSUClientError as exc:
        job_logger.warning(
            f'Failed to fetch reservations for scope(s) {sorted(scope_ids)}: {exc} — '
            f'skipping their reservation push (the scope may not be on the server yet)'
        )
        return {}
    return {sid: grouped[sid] for sid in scope_ids if sid in grouped}


def _push_scope_reservations(job_logger, client, server, scopes, cfg, known=None):
    """
    Reconcile the reservations of `scopes` ({scope_id: DHCPScope}) against `server`.
    `known` gives reservation lists already in hand (a scope just created has none);
    the rest are fetched. Returns (scope IDs with changes, failed change count).
    """
    from .utils import psu_supports_reservation_by_ip

    known = dict(known or {})
    by_ip = psu_supports_reservation_by_ip(server)
    if scopes and not by_ip:
        job_logger.warning(
            f'Server {server.name}: PSU script v{server.psu_script_version or "unknown"} can\'t '
            f'update or delete reservations — only creating missing ones. Run "Update PSU '
            f'Scripts" to enable the full reservation push.'
        )
    known.update(_fetch_scope_reservations(
        job_logger, client, [sid for sid in scopes if sid not in known],
    ))
    protected = _protected_prefix_networks(cfg)
    protect_tag = cfg.sync_protect_tag.slug if cfg.sync_protect_tag_id else ''

    changed, failed = set(), 0
    for scope_id, scope in scopes.items():
        if scope_id not in known:
            job_logger.info(f'Scope "{scope.name}": not on the server yet — skipping its reservations')
            continue
        res_changed, res_failed = _reconcile_reservations(
            job_logger, client, scope, scope_id, known[scope_id],
            reservation_status=cfg.reservation_status, by_ip=by_ip,
            placeholders=cfg.reservation_placeholders, protect_tag=protect_tag,
            protected_prefix_networks=protected,
        )
        failed += res_failed
        if res_changed:
            changed.add(scope_id)
    return changed, failed


def _replicate(job_logger, client, scope_ids):
    from .api_client import PSUClientError

    if not scope_ids:
        return
    try:
        client.replicate_failover(scope_ids=sorted(scope_ids))
        job_logger.info(
            f'Replicated failover state for {len(scope_ids)} scope(s): {sorted(scope_ids)}'
        )
    except PSUClientError as exc:
        job_logger.error(f'Failed to replicate failover state for scope(s) {sorted(scope_ids)}: {exc}')


def _push_scopes(job_logger, client, server, scope_pks, cfg=None):
    """
    Push exactly the given DHCPScope pks to `server` — no reconciliation of any
    other scope, lease, reservation, or exclusion on that server. Used by
    DHCPScopePushJob so that saving a handful of scopes doesn't trigger a full
    reconcile of a server that might have hundreds of unrelated scopes.

    With push_reservations on (`cfg`), each pushed scope's reservations follow in the
    same job, and each changed failover scope is replicated once.
    Returns the number of failed reservation changes.

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
    from .utils import psu_supports_scope_state

    try:
        remote_scopes = client.list_scopes(include_router=False)
    except PSUClientError as exc:
        job_logger.error(f'Failed to fetch scopes from {server.name}: {exc}')
        return 0

    # Every scope, active or not. A pushed scope that overlaps a different server scope
    # isn't created here (the server refuses it); the next full sync removes the
    # server-only scope first, then creates this one.
    remote_scope_map = _remote_scope_map(remote_scopes)
    push_state = psu_supports_scope_state(server)

    failover_replicate_scope_ids = set()
    # Scopes now on the server whose reservations follow; a just-created one has none yet.
    reservation_scopes = {}
    known_reservations = {}

    for scope in DHCPScope.objects.filter(pk__in=scope_pks).select_related('prefix', 'failover'):
        reason = _push_skip_reason(scope, server)
        if reason is not None:
            if reason:
                job_logger.info(reason)
            continue

        scope_id = scope.network
        if not scope_id:
            job_logger.warning(f'Could not determine network address for scope "{scope.name}" — skipping')
            continue

        remote = remote_scope_map.get(scope_id)

        if remote is None:
            scope_pushed = _push_scope(job_logger, client, scope, push_state=push_state)
            if scope_pushed and scope.prefix_id:
                reservation_scopes[scope_id] = scope
                known_reservations[scope_id] = []
        else:
            scope_pushed = _push_scope(job_logger, client, scope, remote=remote, scope_id=scope_id,
                                       push_state=push_state)
            if scope.prefix_id:
                reservation_scopes[scope_id] = scope

        exclusions_changed = _sync_exclusions(job_logger, client, scope, scope_id)
        if scope.failover_id and (scope_pushed or exclusions_changed):
            failover_replicate_scope_ids.add(scope_id)

        DHCPScope.objects.filter(pk=scope.pk).update(last_sync_at=timezone.now())

    failed = 0
    if cfg is not None and cfg.push_reservations and reservation_scopes:
        changed, failed = _push_scope_reservations(
            job_logger, client, server, reservation_scopes, cfg, known=known_reservations,
        )
        failover_replicate_scope_ids |= {
            sid for sid in changed if reservation_scopes[sid].failover_id
        }

    _replicate(job_logger, client, failover_replicate_scope_ids)
    return failed


def _request_user():
    """The signed-in user of the request being handled (UI or API), or None outside one."""
    from netbox.context import current_request

    user = getattr(current_request.get(), 'user', None)
    return user if getattr(user, 'is_authenticated', False) else None


class _PushJobMixin:
    """
    Push jobs are enqueued by signals when someone saves or deletes an object, so
    attribute the job to that person — DHCP-Sync-Service only when there's no request
    (a script, the shell) — and run it on the Sync Job Queue.
    """

    @classmethod
    def enqueue(cls, *args, **kwargs):
        if kwargs.get('user') is None:
            kwargs['user'] = _request_user() or _service_user()
        try:
            kwargs.setdefault('queue_name', _load_settings().sync_queue)
        except Exception:
            # Settings may not be loadable during initial migrations — use NetBox's default queue.
            pass
        return super().enqueue(*args, **kwargs)


class DHCPScopePushJob(_PushJobMixin, JobRunner):
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
        _apply_sync_log_level(self.logger)

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

        if server.access_level == DHCPServer.ACCESS_RO:
            self.logger.info(f'Server {server.name}: API token is read-only — skipping scope push')
            return

        client = PSUClient(server)
        cfg = _load_settings()
        with _change_logging():
            failed = _push_scopes(self.logger, client, server, scope_pks, cfg=cfg)
        if failed:
            _fail_job(self.job, _reservation_failure_summary(server.name, failed))


class DHCPReservationPushJob(_PushJobMixin, JobRunner):
    """
    Push the reservations of specific scopes to a single server — triggered by saving or
    deleting a reservation-status IP when push_reservations=True. Like DHCPScopePushJob,
    it never touches any other scope on the server. A scope that isn't on the server yet,
    or whose reservations can't be fetched, is skipped: the scope push brings its
    reservations.
    """

    class Meta:
        name = 'Windows DHCP Reservation Push'
        description = 'Push the reservations of specific scope(s) to a Windows DHCP server.'

    def __init__(self, job):
        super().__init__(job)
        _quiet_file_log(self.logger)
        _apply_sync_log_level(self.logger)

    def run(self, *args, **kwargs):
        server_pk = kwargs.get('server_pk')
        scope_pks = kwargs.get('scope_pks') or []
        if not server_pk or not scope_pks:
            self.logger.error('No server_pk/scope_pks provided to job.')
            return

        from .api_client import PSUClient
        from .models import DHCPScope, DHCPServer

        cfg = _load_settings()
        if not cfg.push_reservations:
            self.logger.info('Push Reservations is off — nothing to push')
            return

        try:
            server = DHCPServer.objects.get(pk=server_pk)
        except DHCPServer.DoesNotExist:
            self.logger.error(f'DHCPServer pk={server_pk} not found.')
            return

        if server.maintenance_mode:
            self.logger.info(f'Skipping server {server.name}: maintenance mode enabled')
            return

        _check_server_health(self.logger, self.job, server)

        if server.access_level == DHCPServer.ACCESS_RO:
            self.logger.info(f'Server {server.name}: API token is read-only — skipping reservation push')
            return

        scopes = {}
        for scope in DHCPScope.objects.filter(pk__in=scope_pks).select_related('prefix', 'failover'):
            reason = _push_skip_reason(scope, server)
            if reason is not None:
                if reason:
                    self.logger.info(reason)
                continue
            if not scope.prefix_id:
                self.logger.info(f'Scope "{scope.name}": has no prefix — skipping its reservations')
                continue
            scopes[scope.network] = scope
        if not scopes:
            return

        client = PSUClient(server)
        with _change_logging():
            changed, failed = _push_scope_reservations(self.logger, client, server, scopes, cfg)
        _replicate(self.logger, client, {sid for sid in changed if scopes[sid].failover_id})
        if failed:
            _fail_job(self.job, _reservation_failure_summary(server.name, failed))


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
    whether the remote call succeeds. Returns True if the scope was deleted.
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
        return True
    except Exception as exc:
        job_logger.warning(f'Scope {scope_id}: failed to delete from server: {exc}')
        return False


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
    from .utils import AmbiguousFailover, find_failover

    for item in deletes:
        scope_id = item['scope_id']
        scope_name = item.get('scope_name', scope_id)
        if item.get('maintenance_mode'):
            job_logger.info(f'Scope "{scope_name}": in maintenance mode — skipping delete')
            continue

        failover_name = item.get('failover_name')
        if failover_name:
            try:
                failover = find_failover(failover_name, server)
            except AmbiguousFailover as exc:
                job_logger.warning(f'Scope "{scope_name}": {exc} — skipping delete')
                continue
            if failover is not None and not failover.sync_enabled:
                job_logger.info(
                    f'Scope "{scope_name}": failover "{failover_name}" has sync disabled — skipping delete'
                )
                continue
        elif not server.sync_standalone_scopes:
            job_logger.debug(
                f'Scope "{scope_name}": standalone scopes disabled on {server.name} — skipping delete'
            )
            continue

        _deconfigure_and_delete_scope(job_logger, client, scope_id, failover_name=failover_name)


class DHCPScopeDeleteJob(_PushJobMixin, JobRunner):
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
        _apply_sync_log_level(self.logger)

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

        if server.access_level == DHCPServer.ACCESS_RO:
            self.logger.info(f'Server {server.name}: API token is read-only — skipping scope delete')
            return

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

        from .import_logic import run_import
        self.logger.info(f'Starting import from {server.name} ({server.hostname})')
        with _change_logging():
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
            for item in data.get('unassigned', []):
                self.logger.warning(f'  Unassigned: {item}')
            for item in data.get('maintenance', []):
                self.logger.warning(f'  Maintenance: {item}')
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

        from core.exceptions import JobFailed

        from .api_client import PSUClient, PSUClientError
        from .constants import PSU_SCRIPT_VERSION
        from .models import DHCPServer

        def _fail(msg):
            self.logger.error(msg)
            self.job.error = msg
            self.job.save(update_fields=['error'])
            raise JobFailed()

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
            _fail(f'Failed to parse dhcp_api_endpoints.ps1: {exc}')

        self.logger.info(f'Parsed {len(defined_endpoints)} endpoint definition(s) from PS1 file')

        client = PSUClient(server)

        # Fetch current PSU endpoint records
        try:
            current_eps = client.get_dhcp_endpoints()
        except PSUClientError as exc:
            _fail(f'Failed to fetch current PSU endpoints: {exc}')

        self.logger.info(f'Found {len(current_eps)} existing /api/dhcp/ endpoint record(s) in PSU')

        # Build lookup: (url, method) → endpoint record
        current_map = {
            (ep.get('url', ''), m): ep
            for ep in current_eps
            for m in ([ep['method']] if isinstance(ep.get('method'), str) else ep.get('method', []))
        }

        defined_keys = {(d['url'], d['method']) for d in defined_endpoints}
        current_keys = set(current_map.keys())

        errors = []

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
                    msg = f'Failed to update {defn["method"]} {defn["url"]}: {exc}'
                    self.logger.error(msg)
                    errors.append(msg)

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
                    msg = f'Failed to create {defn["method"]} {defn["url"]}: {exc}'
                    self.logger.error(msg)
                    errors.append(msg)

        # Delete removed endpoints (in PSU but not in defined)
        deleted = 0
        for key in (current_keys - defined_keys):
            ep_record = current_map[key]
            try:
                client.delete_endpoint(ep_record['id'])
                self.logger.info(f'Deleted endpoint {key[1]} {key[0]}')
                deleted += 1
            except PSUClientError as exc:
                msg = f'Failed to delete {key[1]} {key[0]}: {exc}'
                self.logger.error(msg)
                errors.append(msg)

        self.logger.info(
            f'Endpoints: {updated} updated, {created} created, {deleted} deleted'
        )

        # Restart endpoint definitions in PSU
        try:
            client.restart_endpoints()
            self.logger.info('PSU endpoints restarted successfully')
        except PSUClientError as exc:
            _fail(f'Failed to restart PSU endpoints: {exc}')

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
            msg = f'Health check after update failed: {exc}'
            self.logger.error(msg)
            server.health_status = DHCPServer.HEALTH_UNREACHABLE
            server.health_error = str(exc)
            server.save(update_fields=['health_status', 'health_error'])
            errors.append(msg)

        if errors:
            self.job.error = '; '.join(errors)
            self.job.save(update_fields=['error'])
            raise JobFailed()
