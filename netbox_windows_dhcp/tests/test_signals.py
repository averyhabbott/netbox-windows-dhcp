"""
Signal-handler tests. Job enqueue methods are patched so no test reaches RQ/Redis.
"""

from unittest import mock

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase
from ipam.models import IPAddress

from ..models import DHCPExclusionRange, DHCPScope
from ..signals import validate_dhcp_ip_status
from .base import make_failover, make_prefix, make_scope, make_server, set_plugin_settings

ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPScopePushJob.enqueue'
DELETE_ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPScopeDeleteJob.enqueue'
ENQUEUE_ONCE = 'netbox_windows_dhcp.background_tasks.DHCPSyncJob.enqueue_once'


class ScopePostSaveSignalTests(TestCase):
    """
    DHCPScope saves defer their push to transaction commit (signals.py's
    _queue_scope_push/_flush_pending_scope_pushes), so every test here runs
    inside captureOnCommitCallbacks(execute=True) — TestCase wraps each test
    in a transaction that's rolled back rather than committed, so on_commit()
    hooks never fire without it.
    """

    def test_enqueues_scope_push_when_push_enabled(self):
        server = make_server()
        set_plugin_settings(push_scope_info=True)
        with mock.patch(ENQUEUE_ONCE), mock.patch(ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                scope = make_scope(server=server)
        enq.assert_called_once_with(server_pk=server.pk, scope_pks=[scope.pk])

    def test_no_enqueue_when_push_disabled(self):
        server = make_server()
        set_plugin_settings(push_scope_info=False)
        with mock.patch(ENQUEUE_ONCE), mock.patch(ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                make_scope(server=server)
        self.assertFalse(enq.called)

    def test_only_targets_scope_own_server(self):
        """A saved scope must only enqueue a push for its own server, not every server."""
        target = make_server(name='Target', hostname='target.example.com')
        make_server(name='Other', hostname='other.example.com')
        set_plugin_settings(push_scope_info=True)
        with mock.patch(ENQUEUE_ONCE), mock.patch(ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                make_scope(server=target)
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs['server_pk'], target.pk)

    def test_failover_scope_targets_only_primary(self):
        """
        Regression test: a failover scope must only enqueue a push against the
        primary — never the secondary directly. Windows failover replication
        (triggered by the push itself) is what propagates the change to the
        secondary; pushing to both risked a redundant/conflicting direct write.
        """
        primary = make_server(name='Primary A', hostname='primary-a.example.com')
        secondary = make_server(name='Secondary A', hostname='secondary-a.example.com')
        failover = make_failover(primary=primary, secondary=secondary)
        set_plugin_settings(push_scope_info=True)
        with mock.patch(ENQUEUE_ONCE), mock.patch(ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                make_scope(failover=failover)
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs['server_pk'], primary.pk)

    def test_multiple_saves_in_one_transaction_batch_into_one_job_per_server(self):
        """
        Regression test: N scope saves within a single committed transaction
        (e.g. a bulk edit) must enqueue exactly one DHCPScopePushJob per
        affected server, carrying every changed scope's pk — not one job per
        save, which would fire a wasteful burst of jobs against a server with
        many unrelated scopes.
        """
        server = make_server()
        set_plugin_settings(push_scope_info=True)
        with mock.patch(ENQUEUE_ONCE), mock.patch(ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    scope_a = make_scope(name='A', server=server)
                    scope_b = make_scope(name='B', server=server)
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs['server_pk'], server.pk)
        self.assertEqual(set(enq.call_args.kwargs['scope_pks']), {scope_a.pk, scope_b.pk})


class ScopePreDeleteSignalTests(TestCase):
    """
    DHCPScope deletes defer their remote cleanup to transaction commit
    (signals.py's _queue_scope_delete/_flush_pending_scope_deletes), mirroring
    the post_save push path — see ScopePostSaveSignalTests's class docstring
    for why captureOnCommitCallbacks(execute=True) is required.
    """

    def test_enqueues_scope_delete_when_push_enabled(self):
        server = make_server()
        scope = make_scope(server=server)
        set_plugin_settings(push_scope_info=True)
        with mock.patch(ENQUEUE_ONCE), mock.patch(DELETE_ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                scope.delete()
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs['server_pk'], server.pk)
        self.assertEqual(enq.call_args.kwargs['deletes'], [{
            'scope_id': '10.0.1.0',
            'scope_name': scope.name,
            'failover_name': None,
            'maintenance_mode': False,
        }])

    def test_no_enqueue_when_push_disabled(self):
        scope = make_scope()
        set_plugin_settings(push_scope_info=False)
        with mock.patch(ENQUEUE_ONCE), mock.patch(DELETE_ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                scope.delete()
        self.assertFalse(enq.called)

    def test_no_enqueue_when_scope_has_neither_server_nor_failover(self):
        # Bypasses DHCPScope.clean()'s "must have one" rule — .objects.create()
        # doesn't call full_clean() — defensive coverage for data that
        # shouldn't exist but is cheap to guard against, mirroring the
        # post_save push path's identical guard.
        scope = DHCPScope.objects.create(
            name='Orphaned', prefix=make_prefix(), server=None, failover=None,
            start_ip='10.0.1.10', end_ip='10.0.1.254',
        )
        set_plugin_settings(push_scope_info=True)
        with mock.patch(ENQUEUE_ONCE), mock.patch(DELETE_ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                scope.delete()
        self.assertFalse(enq.called)

    def test_failover_scope_delete_targets_primary_with_failover_name(self):
        primary = make_server(name='Primary A', hostname='primary-a.example.com')
        secondary = make_server(name='Secondary A', hostname='secondary-a.example.com')
        failover = make_failover(name='FO-A', primary=primary, secondary=secondary)
        scope = make_scope(failover=failover)
        set_plugin_settings(push_scope_info=True)
        with mock.patch(ENQUEUE_ONCE), mock.patch(DELETE_ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                scope.delete()
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs['server_pk'], primary.pk)
        self.assertEqual(enq.call_args.kwargs['deletes'][0]['failover_name'], 'FO-A')

    def test_multiple_deletes_in_one_transaction_batch_into_one_job_per_server(self):
        server = make_server()
        prefix_a = make_prefix('10.0.1.0/24')
        prefix_b = make_prefix('10.0.2.0/24')
        scope_a = make_scope(name='A', prefix=prefix_a, server=server)
        scope_b = make_scope(
            name='B', prefix=prefix_b, server=server,
            start_ip='10.0.2.10', end_ip='10.0.2.254',
        )
        set_plugin_settings(push_scope_info=True)
        with mock.patch(ENQUEUE_ONCE), mock.patch(DELETE_ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    scope_a.delete()
                    scope_b.delete()
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs['server_pk'], server.pk)
        deleted_scope_ids = {d['scope_id'] for d in enq.call_args.kwargs['deletes']}
        self.assertEqual(deleted_scope_ids, {'10.0.1.0', '10.0.2.0'})


class SettingsPostSaveSignalTests(TestCase):
    def test_reschedules_sync_with_new_interval(self):
        with mock.patch(ENQUEUE_ONCE) as enqueue_once:
            set_plugin_settings(sync_interval=120)
        self.assertTrue(enqueue_once.called)
        self.assertEqual(enqueue_once.call_args.kwargs.get('interval'), 120)

    @mock.patch('core.models.jobs.django_rq.get_queue')
    def test_collapses_duplicate_chains_on_save(self, mock_get_queue):
        """
        Regression test: saving settings while duplicate scheduled/pending
        "Windows DHCP Sync" jobs already exist must collapse them to one
        immediately, not just reschedule the single most-recently-created
        row (enqueue_once() alone can't see the others).
        """
        import uuid

        from core.choices import JobStatusChoices
        from core.models import Job

        from ..background_tasks import DHCPSyncJob

        mock_get_queue.return_value = mock.MagicMock()

        def make_job(status):
            return Job.objects.create(
                name=DHCPSyncJob.name, status=status, job_id=uuid.uuid4(), queue_name='default',
            )

        dupes = [
            make_job(JobStatusChoices.STATUS_SCHEDULED),
            make_job(JobStatusChoices.STATUS_SCHEDULED),
            make_job(JobStatusChoices.STATUS_PENDING),
        ]

        set_plugin_settings(sync_interval=120)

        remaining = Job.objects.filter(name=DHCPSyncJob.name)
        self.assertEqual(remaining.count(), 1)
        self.assertEqual(remaining.first().interval, 120)
        for dupe in dupes:
            self.assertFalse(Job.objects.filter(pk=dupe.pk).exists())


class ValidateDHCPIPStatusTests(TestCase):
    """The post_clean handler is called directly with constructed instances."""

    def setUp(self):
        set_plugin_settings(lease_status='dhcp')

    # IPs are saved AND reloaded so the address field is coerced to a netaddr
    # object — the handler reads instance.address.ip and silently bails on an
    # uncoerced (str) value, which a freshly-created in-memory instance still has.
    # Saving does not trigger validation (post_clean only fires on full_clean).
    @staticmethod
    def _make_ip(address, status):
        ip = IPAddress.objects.create(address=address, status=status)
        ip.refresh_from_db()
        return ip

    def test_non_lease_status_is_ignored(self):
        ip = self._make_ip('10.0.1.50/24', 'active')
        validate_dhcp_ip_status(sender=IPAddress, instance=ip)  # no raise

    def test_lease_status_without_scope_raises(self):
        ip = self._make_ip('10.0.1.50/24', 'dhcp')
        with self.assertRaises(ValidationError):
            validate_dhcp_ip_status(sender=IPAddress, instance=ip)

    def test_lease_status_within_scope_ok(self):
        make_scope(start_ip='10.0.1.10', end_ip='10.0.1.254')  # prefix 10.0.1.0/24
        ip = self._make_ip('10.0.1.60/24', 'dhcp')
        validate_dhcp_ip_status(sender=IPAddress, instance=ip)  # no raise

    def test_lease_status_inside_exclusion_raises(self):
        scope = make_scope(start_ip='10.0.1.10', end_ip='10.0.1.254')
        DHCPExclusionRange.objects.create(scope=scope, start_ip='10.0.1.40', end_ip='10.0.1.60')
        ip = self._make_ip('10.0.1.50/24', 'dhcp')
        with self.assertRaises(ValidationError):
            validate_dhcp_ip_status(sender=IPAddress, instance=ip)
