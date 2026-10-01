"""
Signal handlers for netbox-windows-dhcp.

When push_scope_info is enabled, saving a DHCPScope (or one of its exclusion
ranges) automatically enqueues a job to push the updated scope to the DHCP server. When push_reservations is
enabled, saving or deleting a reservation-status IP does the same for the
reservations of its scope.
"""

import logging
import threading

from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver
from netbox.signals import post_clean

logger = logging.getLogger('netbox_windows_dhcp')

class _CommitBatch:
    """
    Work queued during one transaction, handed to `flush_items` once it commits, so a
    bulk edit of N objects enqueues one job per affected server instead of N.

    Each batch belongs to one on_commit() callback: when that transaction rolls back
    (a cancelled save, a failed bulk edit), Django drops the callback, and the next
    queue call starts a fresh batch — so rolled-back saves are never pushed later, and
    a rollback can't leave the queue stuck.

    The batch notes who is making the change when it starts, because the commit can
    land after the request is gone; the jobs it enqueues run as that user.
    """

    def __init__(self, flush_items):
        from .background_tasks import _request_user

        self.items = {}
        self._flush_items = flush_items
        self.user = _request_user()

    def flush(self):
        items, self.items = self.items, {}
        if items:
            self._flush_items(items, self.user)


def _as_user(user):
    """enqueue() kwargs for `user`; with none, the job class picks its own default."""
    return {'user': user} if user is not None else {}


def _commit_batch(local, flush_items):
    """The batch in `local` waiting on the current transaction's commit, starting one if none is."""
    from django.db import connection, transaction

    batch = getattr(local, 'batch', None)
    if batch is not None and any(item[1] == batch.flush for item in connection.run_on_commit):
        return batch
    batch = local.batch = _CommitBatch(flush_items)
    # Outside a transaction this runs at once — on an empty batch, so it's a no-op, and
    # the caller's items are flushed by _flush_now_if_autocommit() instead.
    transaction.on_commit(batch.flush)
    return batch


def _flush_now_if_autocommit(batch):
    from django.db import connection
    if not connection.in_atomic_block:
        batch.flush()


# Scope pushes: {server_pk: {scope_pk, ...}}. Thread-local because a NetBox worker
# handles one request per thread at a time.
_pending = threading.local()


def _flush_scope_pushes(by_server, user=None):
    from .background_tasks import DHCPScopePushJob
    from .models import DHCPScope

    # A scope deleted later in the same transaction (an exclusion deleted along with its
    # scope queues a push for it) has nothing left to push.
    existing = set(DHCPScope.objects.filter(
        pk__in={pk for pks in by_server.values() for pk in pks},
    ).values_list('pk', flat=True))
    for server_id, scope_pks in by_server.items():
        scope_pks = sorted(scope_pks & existing)
        if not scope_pks:
            continue
        try:
            DHCPScopePushJob.enqueue(server_pk=server_id, scope_pks=scope_pks, **_as_user(user))
        except Exception as exc:
            logger.warning(f'Failed to enqueue scope push for server pk={server_id}: {exc}')


def _queue_scope_push(scope):
    """
    Queue `scope` for a push, against the server relevant to it (its own server, or the
    primary of its failover relationship — never the secondary directly; Windows
    failover replication propagates the change to it). The job is created when the
    transaction commits (see _CommitBatch).
    """
    if scope.server_id:
        server_id = scope.server_id
    elif scope.failover_id:
        server_id = scope.failover.primary_server_id
    else:
        return

    batch = _commit_batch(_pending, _flush_scope_pushes)
    batch.items.setdefault(server_id, set()).add(scope.pk)
    _flush_now_if_autocommit(batch)


@receiver(post_save, sender='netbox_windows_dhcp.DHCPScope')
def dhcpscope_post_save(sender, instance, created, **kwargs):
    """When a DHCPScope is saved and push_scope_info is on, push to DHCP server."""
    from .models import DHCPPluginSettings
    if not DHCPPluginSettings.load().push_scope_info:
        return
    try:
        _queue_scope_push(instance)
    except Exception as exc:
        logger.warning(f'Failed to enqueue scope push after save of {instance}: {exc}')


@receiver(post_save, sender='ipam.Prefix')
def prefix_post_save(sender, instance, created, **kwargs):
    """
    Keep the network and prefix length stored on a prefix's scope in step when the
    prefix itself is edited, so the scope ID still follows the prefix.
    """
    if created:
        return
    from netaddr import IPNetwork

    from .models import DHCPScope
    try:
        net = IPNetwork(str(instance.prefix))
    except Exception:
        return
    DHCPScope.objects.filter(prefix=instance).exclude(
        network=str(net.network), prefix_length=net.prefixlen,
    ).update(network=str(net.network), prefix_length=net.prefixlen)


# Scope deletes: {server_pk: [snapshot, ...]}, batched the same way as pushes.
_pending_deletes = threading.local()


def _flush_scope_deletes(by_server, user=None):
    from .background_tasks import DHCPScopeDeleteJob
    for server_id, deletes in by_server.items():
        try:
            DHCPScopeDeleteJob.enqueue(server_pk=server_id, deletes=deletes, **_as_user(user))
        except Exception as exc:
            logger.warning(f'Failed to enqueue scope delete for server pk={server_id}: {exc}')


def _queue_scope_delete(scope):
    """
    Snapshot everything DHCPScopeDeleteJob needs about `scope` *before* it's
    gone — this runs on pre_delete, not post_delete, because the row (and its
    prefix/failover FKs) must still be readable. The job is created when the
    transaction commits (see _CommitBatch), so a bulk delete of N scopes enqueues
    one DHCPScopeDeleteJob per affected server rather than N.
    """
    server_id = None
    failover_name = None
    maintenance_mode = scope.maintenance_mode
    if scope.server_id:
        server_id = scope.server_id
    elif scope.failover_id:
        server_id = scope.failover.primary_server_id
        failover_name = scope.failover.name
        maintenance_mode = maintenance_mode or scope.failover.maintenance_mode
    if not server_id:
        return

    scope_id = scope.network
    if not scope_id:
        logger.warning(f'Could not determine network address for scope {scope} — skipping delete push')
        return

    batch = _commit_batch(_pending_deletes, _flush_scope_deletes)
    batch.items.setdefault(server_id, []).append({
        'scope_id': scope_id,
        'scope_name': scope.name,
        'failover_name': failover_name,
        'maintenance_mode': maintenance_mode,
    })
    _flush_now_if_autocommit(batch)


@receiver(pre_delete, sender='netbox_windows_dhcp.DHCPScope')
def dhcpscope_pre_delete(sender, instance, **kwargs):
    """When a DHCPScope is deleted and push_scope_info is on, remove it from the DHCP server."""
    from .models import DHCPPluginSettings
    if not DHCPPluginSettings.load().push_scope_info:
        return
    try:
        _queue_scope_delete(instance)
    except Exception as exc:
        logger.warning(f'Failed to enqueue scope delete after deleting {instance}: {exc}')


def _exclusion_push_enabled():
    """True if push_scope_info is on and this isn't a plugin job's own write."""
    from .models import DHCPPluginSettings
    from .utils import plugin_write_active
    return not plugin_write_active() and DHCPPluginSettings.load().push_scope_info


def _queue_exclusion_scope_push(*scope_pks):
    """Queue a push of each scope in `scope_pks` (the push syncs its exclusions)."""
    from .models import DHCPScope
    for scope in DHCPScope.objects.filter(pk__in=[pk for pk in scope_pks if pk]).select_related('failover'):
        _queue_scope_push(scope)


@receiver(pre_save, sender='netbox_windows_dhcp.DHCPExclusionRange')
def dhcpexclusionrange_pre_save(sender, instance, **kwargs):
    """Remember the exclusion's stored scope, so post_save can push it too if it moved."""
    instance._dhcp_scope_before_save = None
    if instance.pk and _exclusion_push_enabled():
        from .models import DHCPExclusionRange
        instance._dhcp_scope_before_save = (
            DHCPExclusionRange.objects.filter(pk=instance.pk).values_list('scope_id', flat=True).first()
        )


@receiver(post_save, sender='netbox_windows_dhcp.DHCPExclusionRange')
def dhcpexclusionrange_post_save(sender, instance, **kwargs):
    """
    When push_scope_info is on, saving an exclusion range pushes its scope right away
    (and the old scope, if it moved) instead of waiting for the next sync. The plugin's
    own writes never trigger this.
    """
    before = getattr(instance, '_dhcp_scope_before_save', None)
    instance._dhcp_scope_before_save = None
    if not _exclusion_push_enabled():
        return
    try:
        _queue_exclusion_scope_push(instance.scope_id, before)
    except Exception as exc:
        logger.warning(f'Failed to enqueue scope push after save of exclusion {instance}: {exc}')


@receiver(pre_delete, sender='netbox_windows_dhcp.DHCPExclusionRange')
def dhcpexclusionrange_pre_delete(sender, instance, **kwargs):
    """
    When push_scope_info is on, deleting an exclusion range pushes its scope right away.
    Deleting the whole scope deletes its exclusions too; that push is dropped at commit,
    since the scope is gone (see _flush_scope_pushes).
    """
    if not _exclusion_push_enabled():
        return
    try:
        _queue_exclusion_scope_push(instance.scope_id)
    except Exception as exc:
        logger.warning(f'Failed to enqueue scope push after deleting exclusion {instance}: {exc}')


# Reservation pushes: {server_pk: {scope_pk, ...}}, batched the same way as scope
# pushes, so a bulk edit of N reserved IPs enqueues one DHCPReservationPushJob per
# affected server.
_pending_reservations = threading.local()


def _flush_reservation_pushes(by_server, user=None):
    from .background_tasks import DHCPReservationPushJob
    for server_id, scope_pks in by_server.items():
        try:
            DHCPReservationPushJob.enqueue(
                server_pk=server_id, scope_pks=sorted(scope_pks), **_as_user(user),
            )
        except Exception as exc:
            logger.warning(f'Failed to enqueue reservation push for server pk={server_id}: {exc}')


def _reservation_push_enabled():
    """The plugin settings if push_reservations is on and this isn't a plugin job's own write."""
    from .models import DHCPPluginSettings
    from .utils import plugin_write_active
    if plugin_write_active():
        return None
    cfg = DHCPPluginSettings.load()
    return cfg if cfg.push_reservations else None


def _queue_reservation_push(*locations):
    """
    Queue a reservation push for the scopes containing each (address, vrf_id) in
    `locations` (address: IP or CIDR string), against the scope's server or its
    failover's primary.
    """
    from .utils import scope_for_ip

    targets = []
    for address, vrf_id in locations:
        scope = scope_for_ip(address, vrf_id)
        if scope is None:
            continue
        if scope.server_id:
            targets.append((scope.server_id, scope.pk))
        elif scope.failover_id:
            targets.append((scope.failover.primary_server_id, scope.pk))
    if not targets:
        return

    batch = _commit_batch(_pending_reservations, _flush_reservation_pushes)
    for server_id, scope_pk in targets:
        batch.items.setdefault(server_id, set()).add(scope_pk)
    _flush_now_if_autocommit(batch)


@receiver(pre_save, sender='ipam.IPAddress')
def ipaddress_pre_save(sender, instance, **kwargs):
    """Remember the IP's stored address, VRF and status, so post_save can tell if it moved."""
    if not instance.pk or not _reservation_push_enabled():
        return
    from ipam.models import IPAddress
    instance._dhcp_before_save = IPAddress.objects.filter(pk=instance.pk).values(
        'address', 'vrf_id', 'status',
    ).first()


@receiver(post_save, sender='ipam.IPAddress')
def ipaddress_post_save(sender, instance, **kwargs):
    """
    When push_reservations is on and a reservation-status IP is saved (or an IP stops or
    starts being one), push the affected scopes' reservations — the old scope too, if
    the IP moved. The sync's own writes never trigger this.
    """
    cfg = _reservation_push_enabled()
    if cfg is None:
        return
    before = getattr(instance, '_dhcp_before_save', None)
    instance._dhcp_before_save = None
    locations = []
    if instance.status == cfg.reservation_status:
        locations.append((instance.address, instance.vrf_id))
    if before and before['status'] == cfg.reservation_status:
        locations.append((before['address'], before['vrf_id']))
    try:
        _queue_reservation_push(*locations)
    except Exception as exc:
        logger.warning(f'Failed to enqueue reservation push after save of {instance}: {exc}')


@receiver(pre_delete, sender='ipam.IPAddress')
def ipaddress_pre_delete(sender, instance, **kwargs):
    """When push_reservations is on and a reservation-status IP is deleted, push its scope."""
    cfg = _reservation_push_enabled()
    if cfg is None or instance.status != cfg.reservation_status:
        return
    try:
        _queue_reservation_push((instance.address, instance.vrf_id))
    except Exception as exc:
        logger.warning(f'Failed to enqueue reservation push after deleting {instance}: {exc}')


@receiver(post_clean)
def validate_dhcp_ip_status(sender, instance, **kwargs):
    """
    Validate that IPs with status 'dhcp' fall within a configured DHCP scope in the
    same VRF and are not inside an exclusion range, and apply the IP lock (see locks.py).

    Runs during form and API validation (full_clean → clean → post_clean signal)
    but NOT during direct .save() calls from the background sync — intentional,
    as the sync writes authoritative data from the DHCP server.
    """
    from ipam.models import IPAddress
    if not isinstance(instance, IPAddress):
        return
    from .locks import check_ip_address
    from .models import DHCPPluginSettings
    from .utils import scope_for_ip
    check_ip_address(instance)
    lease_status = DHCPPluginSettings.load().lease_status
    if instance.status != lease_status:
        return

    from netaddr import IPAddress as NetAddrIP

    try:
        ip = NetAddrIP(str(instance.address.ip))
    except Exception:
        return

    matching_scope = scope_for_ip(ip, instance.vrf_id)

    if matching_scope is None:
        raise ValidationError(
            f'IP addresses with status "{lease_status}" must fall within a configured DHCP '
            f'scope prefix in the same VRF. No matching scope found for {instance.address.ip}.'
        )

    for ex in matching_scope.exclusion_ranges.all():
        try:
            if NetAddrIP(ex.start_ip) <= ip <= NetAddrIP(ex.end_ip):
                raise ValidationError(
                    f'{instance.address.ip} falls within exclusion range '
                    f'{ex.start_ip}–{ex.end_ip} of scope "{matching_scope.name}". '
                    f'Excluded IPs cannot have status "{lease_status}".'
                )
        except ValidationError:
            raise
        except Exception:
            continue
