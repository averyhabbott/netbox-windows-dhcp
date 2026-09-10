"""
Signal handlers for netbox-windows-dhcp.

When push_scope_info is enabled, saving a DHCPScope automatically enqueues
a job to push the updated scope to the DHCP server.
"""

import logging
import threading

from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from netbox.signals import post_clean

logger = logging.getLogger('netbox_windows_dhcp')

# Accumulates {server_pk: {scope_pk, ...}} across every DHCPScope save in the
# current request/transaction, so a bulk edit of N scopes enqueues one
# DHCPScopePushJob per affected server (carrying all N scope pks) instead of
# N separate jobs. Thread-local because a NetBox worker handles one request
# per thread at a time. See _queue_scope_push()/_flush_pending_scope_pushes().
_pending = threading.local()


def _pending_by_server():
    if not hasattr(_pending, 'by_server'):
        _pending.by_server = {}
    return _pending.by_server


def _queue_scope_push(scope):
    """
    Record that `scope` needs pushing, against the server relevant to it (its
    own server, or the primary of its failover relationship — never the
    secondary directly; Windows failover replication propagates the change to
    it), and defer the actual job creation to transaction commit — see module
    docstring on `_pending`. Registering on_commit() unconditionally on every
    call is intentional: since callbacks only run after the whole transaction
    commits, every scope saved beforehand is already accumulated by the time
    the first one runs, so it flushes everything and the rest are no-ops
    against an already-empty dict.
    """
    from django.db import transaction

    server_ids = set()
    if scope.server_id:
        server_ids.add(scope.server_id)
    elif scope.failover_id:
        server_ids.add(scope.failover.primary_server_id)
    if not server_ids:
        return

    pending = _pending_by_server()
    is_first = not pending
    for server_id in server_ids:
        pending.setdefault(server_id, set()).add(scope.pk)

    if is_first:
        transaction.on_commit(_flush_pending_scope_pushes)


def _flush_pending_scope_pushes():
    pending = _pending_by_server()
    if not pending:
        return
    batch = pending.copy()
    pending.clear()

    from .background_tasks import DHCPScopePushJob
    for server_id, scope_pks in batch.items():
        try:
            DHCPScopePushJob.enqueue(server_pk=server_id, scope_pks=sorted(scope_pks))
        except Exception as exc:
            logger.warning(f'Failed to enqueue scope push for server pk={server_id}: {exc}')


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


# Same accumulate-then-flush-on-commit shape as _pending/_queue_scope_push
# above, kept as a separate thread-local since deletes and pushes flush
# independently of each other.
_pending_deletes = threading.local()


def _pending_deletes_by_server():
    if not hasattr(_pending_deletes, 'by_server'):
        _pending_deletes.by_server = {}
    return _pending_deletes.by_server


def _queue_scope_delete(scope):
    """
    Snapshot everything DHCPScopeDeleteJob needs about `scope` *before* it's
    gone — this runs on pre_delete, not post_delete, because the row (and its
    prefix/failover FKs) must still be readable. Defers job creation to
    transaction commit via the same accumulator pattern as
    _queue_scope_push, so a bulk delete of N scopes enqueues one
    DHCPScopeDeleteJob per affected server rather than N.
    """
    from django.db import transaction

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

    try:
        scope_id = str(scope.prefix.prefix.network)
    except Exception:
        logger.warning(f'Could not determine network address for scope {scope} — skipping delete push')
        return

    pending = _pending_deletes_by_server()
    is_first = not pending
    pending.setdefault(server_id, []).append({
        'scope_id': scope_id,
        'scope_name': scope.name,
        'failover_name': failover_name,
        'maintenance_mode': maintenance_mode,
    })

    if is_first:
        transaction.on_commit(_flush_pending_scope_deletes)


def _flush_pending_scope_deletes():
    pending = _pending_deletes_by_server()
    if not pending:
        return
    batch = pending.copy()
    pending.clear()

    from .background_tasks import DHCPScopeDeleteJob
    for server_id, deletes in batch.items():
        try:
            DHCPScopeDeleteJob.enqueue(server_pk=server_id, deletes=deletes)
        except Exception as exc:
            logger.warning(f'Failed to enqueue scope delete for server pk={server_id}: {exc}')


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


@receiver(post_save, sender='netbox_windows_dhcp.DHCPPluginSettings')
def dhcppluginsettings_post_save(sender, instance, created, **kwargs):
    """
    Reschedule the recurring DHCPSyncJob with the latest interval whenever
    plugin settings are saved.

    Goes through converge_schedule() rather than enqueue_once() directly:
    enqueue_once() alone only ever inspects the single most-recently-created
    scheduled/pending job of this name, so if duplicate chains already exist
    (e.g. from repeated rqworker restarts) it can't see or clean up the
    extras. converge_schedule() prunes all duplicates down to one first
    (advisory-locked, per-instance delete so the Redis entry is canceled
    too), then applies the requested interval to the survivor via
    enqueue_once() semantics — so fixing the interval from the UI collapses
    duplicates immediately instead of waiting for the next run() cycle.
    """
    try:
        from .background_tasks import DHCPSyncJob
        DHCPSyncJob.converge_schedule(interval=instance.sync_interval)
    except Exception as exc:
        logger.warning(f'Failed to reschedule DHCPSyncJob after settings save: {exc}')


@receiver(post_clean)
def validate_dhcp_ip_status(sender, instance, **kwargs):
    """
    Validate that IPs with status 'dhcp' fall within a configured DHCP scope
    and are not inside an exclusion range.

    Runs during form and API validation (full_clean → clean → post_clean signal)
    but NOT during direct .save() calls from the background sync — intentional,
    as the sync writes authoritative data from the DHCP server.
    """
    from ipam.models import IPAddress
    if not isinstance(instance, IPAddress):
        return
    from .models import DHCPPluginSettings, DHCPScope
    lease_status = DHCPPluginSettings.load().lease_status
    if instance.status != lease_status:
        return

    from netaddr import IPAddress as NetAddrIP, IPNetwork

    try:
        ip = NetAddrIP(str(instance.address.ip))
    except Exception:
        return

    matching_scope = None
    for scope in DHCPScope.objects.select_related('prefix').prefetch_related('exclusion_ranges'):
        try:
            if ip in IPNetwork(str(scope.prefix.prefix)):
                matching_scope = scope
                break
        except Exception:
            continue

    if matching_scope is None:
        raise ValidationError(
            f'IP addresses with status "{lease_status}" must fall within a configured DHCP '
            f'scope prefix. No matching scope found for {instance.address.ip}.'
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
