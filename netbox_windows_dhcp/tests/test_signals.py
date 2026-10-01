"""
Signal-handler tests. Job enqueue methods are patched so no test reaches RQ/Redis.
"""

from unittest import mock

from django.db import transaction
from django.test import TestCase

from ..models import DHCPExclusionRange, DHCPScope
from .base import make_failover, make_prefix, make_scope, make_server, set_plugin_settings


ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPScopePushJob.enqueue'
DELETE_ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPScopeDeleteJob.enqueue'
ENQUEUE_ONCE = 'netbox_windows_dhcp.background_tasks.DHCPSyncJob.enqueue_once'


class ScopePostSaveSignalTests(TestCase):
    """
    DHCPScope saves defer their push to transaction commit (signals.py's
    _queue_scope_push/_CommitBatch), so every test here runs
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


    def test_a_rolled_back_save_never_blocks_the_next_push(self):
        """
        Regression test: a save whose transaction rolls back (a cancelled save, a failed
        bulk edit) used to leave its scope queued with no commit hook, so no later save
        in that worker thread was ever pushed.
        """
        server = make_server()
        set_plugin_settings(push_scope_info=True)
        with mock.patch(ENQUEUE_ONCE), mock.patch(ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(RuntimeError):
                    with transaction.atomic():
                        make_scope(name='Cancelled', server=server)
                        raise RuntimeError('cancelled')
                scope = make_scope(
                    name='Saved', server=server, prefix=make_prefix('10.0.2.0/24'),
                    start_ip='10.0.2.10', end_ip='10.0.2.254',
                )
        enq.assert_called_once_with(server_pk=server.pk, scope_pks=[scope.pk])


class ScopePreDeleteSignalTests(TestCase):
    """
    DHCPScope deletes defer their remote cleanup to transaction commit
    (signals.py's _queue_scope_delete/_CommitBatch), mirroring
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


    def test_a_rolled_back_delete_never_blocks_the_next_delete(self):
        server = make_server()
        cancelled = make_scope(name='Cancelled', server=server)
        scope = make_scope(
            name='Deleted', server=server, prefix=make_prefix('10.0.2.0/24'),
            start_ip='10.0.2.10', end_ip='10.0.2.254',
        )
        set_plugin_settings(push_scope_info=True)
        with mock.patch(ENQUEUE_ONCE), mock.patch(DELETE_ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(RuntimeError):
                    with transaction.atomic():
                        cancelled.delete()
                        raise RuntimeError('cancelled')
                scope.delete()
        enq.assert_called_once()
        self.assertEqual([d['scope_id'] for d in enq.call_args.kwargs['deletes']], ['10.0.2.0'])


class ExclusionRangeSignalTests(TestCase):
    """Phase 16: with push_scope_info on, an exclusion change pushes its scope right away."""

    def setUp(self):
        # Fixtures are made with push off: a push queued here would join a batch whose
        # commit hook predates each test's captureOnCommitCallbacks().
        set_plugin_settings(push_scope_info=False)
        self.server = make_server()
        self.scope = make_scope(server=self.server)
        self.other = make_scope(
            name='Other', server=self.server, prefix=make_prefix('10.0.2.0/24'),
            start_ip='10.0.2.10', end_ip='10.0.2.254',
        )
        self.exclusion = DHCPExclusionRange.objects.create(
            scope=self.scope, start_ip='10.0.1.20', end_ip='10.0.1.30',
        )
        set_plugin_settings(push_scope_info=True)

    def _pushes(self, change):
        with mock.patch(ENQUEUE_ONCE), mock.patch(ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                change()
        return [(c.kwargs['server_pk'], c.kwargs['scope_pks']) for c in enq.call_args_list]

    def test_create_pushes_the_scope(self):
        pushes = self._pushes(lambda: DHCPExclusionRange.objects.create(
            scope=self.scope, start_ip='10.0.1.40', end_ip='10.0.1.45',
        ))
        self.assertEqual(pushes, [(self.server.pk, [self.scope.pk])])

    def test_edit_pushes_the_scope(self):
        def edit():
            self.exclusion.end_ip = '10.0.1.35'
            self.exclusion.save()
        self.assertEqual(self._pushes(edit), [(self.server.pk, [self.scope.pk])])

    def test_delete_pushes_the_scope(self):
        self.assertEqual(self._pushes(self.exclusion.delete), [(self.server.pk, [self.scope.pk])])

    def test_moving_it_to_another_scope_pushes_both(self):
        def move():
            self.exclusion.scope = self.other
            self.exclusion.start_ip, self.exclusion.end_ip = '10.0.2.20', '10.0.2.30'
            self.exclusion.save()
        self.assertEqual(self._pushes(move), [(self.server.pk, sorted([self.scope.pk, self.other.pk]))])

    def test_failover_scope_pushes_to_the_primary(self):
        primary = make_server(name='Primary A', hostname='primary-a.example.com')
        secondary = make_server(name='Secondary A', hostname='secondary-a.example.com')
        set_plugin_settings(push_scope_info=False)
        scope = make_scope(
            name='FO', failover=make_failover(primary=primary, secondary=secondary),
            prefix=make_prefix('10.0.3.0/24'), start_ip='10.0.3.10', end_ip='10.0.3.254',
        )
        set_plugin_settings(push_scope_info=True)
        pushes = self._pushes(lambda: DHCPExclusionRange.objects.create(
            scope=scope, start_ip='10.0.3.20', end_ip='10.0.3.30',
        ))
        self.assertEqual(pushes, [(primary.pk, [scope.pk])])

    def test_nothing_is_queued_with_push_scope_info_off(self):
        set_plugin_settings(push_scope_info=False)
        self.assertEqual(self._pushes(lambda: DHCPExclusionRange.objects.create(
            scope=self.scope, start_ip='10.0.1.40', end_ip='10.0.1.45',
        )), [])
        self.assertEqual(self._pushes(self.exclusion.delete), [])

    def test_plugin_writes_never_trigger_it(self):
        from ..utils import plugin_write

        def sync_write():
            with plugin_write():
                DHCPExclusionRange.objects.create(scope=self.scope, start_ip='10.0.1.40', end_ip='10.0.1.45')
        self.assertEqual(self._pushes(sync_write), [])

    def test_deleting_the_scope_pushes_nothing(self):
        with mock.patch(DELETE_ENQUEUE) as delete_enq:
            self.assertEqual(self._pushes(self.scope.delete), [])
        delete_enq.assert_called_once()

    def test_a_bulk_change_makes_one_push_per_server(self):
        def bulk():
            with transaction.atomic():
                DHCPExclusionRange.objects.create(scope=self.scope, start_ip='10.0.1.40', end_ip='10.0.1.45')
                DHCPExclusionRange.objects.create(scope=self.other, start_ip='10.0.2.40', end_ip='10.0.2.45')
                self.exclusion.delete()
        self.assertEqual(self._pushes(bulk), [(self.server.pk, sorted([self.scope.pk, self.other.pk]))])


class SettingsPostSaveSignalTests(TestCase):
    """Saving plugin settings must never create, reschedule, or otherwise
    touch a job — a changed interval only takes effect via "Run Now"/
    "Schedule" or the chain's own next reschedule (DHCPSyncJob.enqueue())."""

    @mock.patch('netbox_windows_dhcp.background_tasks.DHCPSyncJob.enqueue')
    @mock.patch(ENQUEUE_ONCE)
    def test_settings_save_never_touches_any_job(self, enqueue_once, enqueue):
        set_plugin_settings(sync_interval=120)

        self.assertFalse(enqueue_once.called)
        self.assertFalse(enqueue.called)
