"""
Background jobs: scheduling, who a job runs as, and jobs that fail or skip.
"""

from datetime import timedelta
from unittest import mock
import uuid

from core.choices import JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job
from django.test import TestCase
from django.utils import timezone

from ..api_client import PSUClientError
from ..background_tasks import (
    DHCPPSUUpdateJob,
    DHCPReservationPushJob,
    DHCPScopeDeleteJob,
    DHCPScopePushJob,
    DHCPServerSyncJob,
    DHCPSyncJob,
    SetDHCPSyncScheduleJob,
)
from ..constants import PSU_SCRIPT_VERSION
from ..models import DHCPServer
from .base import (
    PSU_CLIENT,
    make_failover,
    make_job,
    make_prefix,
    make_scope,
    make_server,
    reserved_ip,
    set_plugin_settings,
)
from .fixtures import FAKE_SCOPE_SNAKE, FakePSUClient


GET_QUEUE = 'core.models.jobs.django_rq.get_queue'
SCOPE_ID = '10.0.1.0'
BY_IP = 'netbox_windows_dhcp.utils.psu_supports_reservation_by_ip'


def _make_sync_job(status=JobStatusChoices.STATUS_SCHEDULED, name=None, **kwargs):
    return Job.objects.create(
        name=name or DHCPSyncJob.name,
        status=status,
        job_id=uuid.uuid4(),
        queue_name='default',
        **kwargs,
    )


class DHCPSyncJobRunConvergenceTests(TestCase):
    """DHCPSyncJob.run() must collapse duplicate chains on every cycle."""

    def setUp(self):
        set_plugin_settings(sync_interval=60)

    @mock.patch(GET_QUEUE)
    def test_run_collapses_duplicate_chains_to_one(self, mock_get_queue):
        mock_queue = mock.MagicMock()
        mock_queue.fetch_job.return_value = None
        mock_get_queue.return_value = mock_queue

        # The job actually executing run() right now — production state has
        # this as 'running' (set by handle()'s job.start() before run()).
        # interval must be truthy: convergence only runs for recurring-chain
        # members (see test_run_of_a_one_off_job_never_converges below).
        current = _make_sync_job(status=JobStatusChoices.STATUS_RUNNING, interval=60)

        # Duplicate siblings in a mix of pending/scheduled statuses.
        siblings = [
            _make_sync_job(status=JobStatusChoices.STATUS_SCHEDULED),
            _make_sync_job(status=JobStatusChoices.STATUS_SCHEDULED),
            _make_sync_job(status=JobStatusChoices.STATUS_PENDING),
            _make_sync_job(status=JobStatusChoices.STATUS_PENDING),
            _make_sync_job(status=JobStatusChoices.STATUS_SCHEDULED),
        ]

        # A job of a different name must never be touched by the cleanup.
        other_job = _make_sync_job(status=JobStatusChoices.STATUS_SCHEDULED, name='Windows DHCP Server Sync')

        # No DHCPServer objects exist, so run() logs "nothing to sync" and
        # returns right after the convergence block — this only exercises
        # the top-of-run() cleanup, not the sync engine.
        DHCPSyncJob(current).run()

        remaining_sync_jobs = Job.objects.filter(name=DHCPSyncJob.name)
        self.assertEqual(remaining_sync_jobs.count(), 1)
        self.assertEqual(remaining_sync_jobs.first().pk, current.pk)
        for sibling in siblings:
            self.assertFalse(Job.objects.filter(pk=sibling.pk).exists())
        self.assertTrue(Job.objects.filter(pk=other_job.pk).exists())

    @mock.patch(GET_QUEUE)
    def test_run_never_deletes_a_running_sibling(self, mock_get_queue):
        mock_get_queue.return_value = mock.MagicMock()
        current = _make_sync_job(status=JobStatusChoices.STATUS_RUNNING, interval=60)
        other_running = _make_sync_job(status=JobStatusChoices.STATUS_RUNNING)

        DHCPSyncJob(current).run()

        self.assertTrue(Job.objects.filter(pk=other_running.pk).exists())

    @mock.patch(GET_QUEUE)
    def test_run_with_no_duplicates_is_a_noop(self, mock_get_queue):
        mock_get_queue.return_value = mock.MagicMock()
        current = _make_sync_job(status=JobStatusChoices.STATUS_RUNNING, interval=60)

        DHCPSyncJob(current).run()

        self.assertTrue(Job.objects.filter(pk=current.pk).exists())

    @mock.patch(GET_QUEUE)
    def test_run_of_a_one_off_job_never_converges(self, mock_get_queue):
        """
        Regression test: a one-off run (interval=None, e.g. "Run Now") must
        never touch the recurring chain's own scheduled entry — convergence
        is gated on self.job.interval so only recurring-chain members
        (always a truthy interval) collapse siblings.
        """
        mock_get_queue.return_value = mock.MagicMock()
        one_off = _make_sync_job(status=JobStatusChoices.STATUS_RUNNING, interval=None)
        recurring_successor = _make_sync_job(status=JobStatusChoices.STATUS_SCHEDULED, interval=60)

        DHCPSyncJob(one_off).run()

        self.assertTrue(Job.objects.filter(pk=recurring_successor.pk).exists())


class ConvergeScheduleTests(TestCase):
    """DHCPSyncJob.converge_schedule() — used by SetDHCPSyncScheduleJob's
    recurring path (the "Schedule" button) to collapse duplicates
    immediately, without waiting for the next run() cycle."""

    @mock.patch(GET_QUEUE)
    def test_converge_schedule_collapses_duplicates_and_keeps_earliest(self, mock_get_queue):
        mock_get_queue.return_value = mock.MagicMock()

        now = timezone.now()
        earliest = _make_sync_job(status=JobStatusChoices.STATUS_SCHEDULED, scheduled=now + timedelta(minutes=5))
        later_one = _make_sync_job(status=JobStatusChoices.STATUS_SCHEDULED, scheduled=now + timedelta(minutes=30))
        pending_one = _make_sync_job(status=JobStatusChoices.STATUS_PENDING, scheduled=None)

        DHCPSyncJob.converge_schedule()

        remaining = Job.objects.filter(name=DHCPSyncJob.name)
        self.assertEqual(remaining.count(), 1)
        self.assertEqual(remaining.first().pk, earliest.pk)
        self.assertFalse(Job.objects.filter(pk=later_one.pk).exists())
        self.assertFalse(Job.objects.filter(pk=pending_one.pk).exists())


class DHCPSyncJobEnqueueAttributionTests(TestCase):
    """
    DHCPSyncJob.enqueue() must always attribute the job to DHCP-Sync-Service
    regardless of caller, and must force-refresh `interval` to the live
    setting on every call except an explicit one-off (interval=None) — see
    the docstring on DHCPSyncJob.enqueue() for why a plain setdefault() isn't
    enough for interval.
    """

    def setUp(self):
        set_plugin_settings(sync_interval=45, sync_queue='default', sync_job_timeout=600)

    @mock.patch('netbox.jobs.JobRunner.enqueue')
    def test_user_is_always_the_service_account_regardless_of_caller(self, mock_super_enqueue):
        from django.contrib.auth import get_user_model

        human = get_user_model().objects.create_user('someone', password='x')

        DHCPSyncJob.enqueue(user=human)

        service_user = get_user_model().objects.get(username='DHCP-Sync-Service')
        self.assertEqual(mock_super_enqueue.call_args.kwargs['user'], service_user)

    @mock.patch('netbox.jobs.JobRunner.enqueue')
    def test_explicit_one_off_interval_is_preserved(self, mock_super_enqueue):
        DHCPSyncJob.enqueue(interval=None)

        self.assertIsNone(mock_super_enqueue.call_args.kwargs['interval'])

    @mock.patch('netbox.jobs.JobRunner.enqueue')
    def test_interval_always_comes_from_the_live_setting(self, mock_super_enqueue):
        # None: a first enqueue. 999: JobRunner's auto-reschedule forwarding the old job's interval.
        for interval in (None, 999):
            with self.subTest(interval=interval):
                DHCPSyncJob.enqueue(**({} if interval is None else {'interval': interval}))
                self.assertEqual(mock_super_enqueue.call_args.kwargs['interval'], 45)


class PushJobEnqueueAttributionTests(TestCase):
    """
    The push jobs are enqueued by signals when someone saves or deletes an object: they
    run as that person (DHCP-Sync-Service when there's no request) on the Sync Job Queue.
    """

    PUSH_JOBS = (DHCPScopePushJob, DHCPScopeDeleteJob, DHCPReservationPushJob)

    def setUp(self):
        set_plugin_settings(sync_queue='low')

    @mock.patch('netbox.jobs.JobRunner.enqueue')
    def test_runs_as_the_requesting_user(self, mock_super_enqueue):
        from django.contrib.auth import get_user_model
        from netbox.context import current_request

        human = get_user_model().objects.create_user('someone', password='x')
        token = current_request.set(mock.Mock(user=human))
        try:
            for job_cls in self.PUSH_JOBS:
                with self.subTest(job=job_cls.__name__):
                    job_cls.enqueue(server_pk=1)
                    self.assertEqual(mock_super_enqueue.call_args.kwargs['user'], human)
        finally:
            current_request.reset(token)

    @mock.patch('netbox.jobs.JobRunner.enqueue')
    def test_falls_back_to_service_account_without_a_request(self, mock_super_enqueue):
        from django.contrib.auth import get_user_model

        service_user = get_user_model().objects.get(username='DHCP-Sync-Service')
        for job_cls in self.PUSH_JOBS:
            with self.subTest(job=job_cls.__name__):
                job_cls.enqueue(server_pk=1)
                self.assertEqual(mock_super_enqueue.call_args.kwargs['user'], service_user)

    @mock.patch('netbox.jobs.JobRunner.enqueue')
    def test_uses_sync_job_queue_but_not_its_timeout(self, mock_super_enqueue):
        for job_cls in self.PUSH_JOBS:
            with self.subTest(job=job_cls.__name__):
                job_cls.enqueue(server_pk=1)
                self.assertEqual(mock_super_enqueue.call_args.kwargs['queue_name'], 'low')
                self.assertNotIn('job_timeout', mock_super_enqueue.call_args.kwargs)

    @mock.patch('netbox.jobs.JobRunner.enqueue')
    def test_ui_delete_attributes_the_job_to_the_user(self, mock_super_enqueue):
        from django.contrib.auth import get_user_model
        from django.test import Client
        from django.urls import reverse

        admin = get_user_model().objects.create_superuser('admin2', password='x')
        server = make_server()
        scope = make_scope(server=server)
        set_plugin_settings(push_scope_info=True)
        client = Client()
        client.force_login(admin)
        with mock.patch('netbox_windows_dhcp.background_tasks.DHCPSyncJob.enqueue_once'):
            with self.captureOnCommitCallbacks(execute=True):
                client.post(
                    reverse('plugins:netbox_windows_dhcp:dhcpscope_delete', args=[scope.pk]),
                    {'confirm': True},
                )
        # NetBox 4.7+ also queues its own search-index job through JobRunner.enqueue.
        pushes = [c for c in mock_super_enqueue.call_args_list if 'server_pk' in c.kwargs]
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0].kwargs['user'], admin)


class ScheduledSyncStandInTests(TestCase):
    """The scheduled sync's health check decides which servers sync, and hands a failover
    to its secondary only when the primary is down and the secondary is healthy."""

    @classmethod
    def setUpTestData(cls):
        cls.primary = make_server(name='Primary', hostname='p.example.com')
        cls.secondary = make_server(name='Secondary', hostname='s.example.com')
        cls.failover = make_failover(name='FO', primary=cls.primary, secondary=cls.secondary)

    def _run(self, clients):
        """Run the scheduled sync; return {server name: fallback failover pks} for each server synced."""
        synced = {}

        def record(logger, server, *args, **kwargs):
            synced[server.name] = kwargs['fallback_failover_ids']

        runner = DHCPSyncJob(make_job(DHCPSyncJob.name))
        runner.logger = mock.Mock()
        with mock.patch(PSU_CLIENT, side_effect=lambda server: clients[server.name]), \
                mock.patch('netbox_windows_dhcp.background_tasks._sync_server', side_effect=record):
            try:
                runner.run()
            except JobFailed:  # an unreachable server fails the run after the others sync
                pass
        return synced, runner.logger

    def test_who_handles_the_failover(self):
        up = FakePSUClient
        down = lambda: FakePSUClient(ping_read_error=PSUClientError('connection refused'))  # noqa: E731
        fo = {self.failover.pk}
        cases = (
            ('both healthy', up(), up(), (), {'Primary': set(), 'Secondary': set()}),
            ('primary unreachable', down(), up(), (), {'Secondary': fo}),
            ('primary in maintenance', up(), up(), ('Primary',), {'Secondary': fo}),
            ('both unreachable', down(), down(), (), {}),
            ('primary unreachable, secondary in maintenance', down(), up(), ('Secondary',), {}),
        )
        for label, primary, secondary, maintenance, expected in cases:
            with self.subTest(label):
                # A server in maintenance skips the health check, so it keeps its last status.
                DHCPServer.objects.update(maintenance_mode=False, health_status=DHCPServer.HEALTH_HEALTHY)
                DHCPServer.objects.filter(name__in=maintenance).update(maintenance_mode=True)
                synced, _ = self._run({'Primary': primary, 'Secondary': secondary})
                self.assertEqual(synced, expected)

    def test_old_psu_script_is_warned_about_and_still_synced(self):
        synced, logger = self._run({'Primary': FakePSUClient(health={'version': '1.1.2'}),
                                    'Secondary': FakePSUClient()})
        self.assertEqual(set(synced), {'Primary', 'Secondary'})
        warnings = [c.args[0] for c in logger.warning.call_args_list]
        self.assertEqual([w for w in warnings if '1.1.2' in w and 'Primary' in w and PSU_SCRIPT_VERSION in w],
                         warnings)
        self.assertEqual(len(warnings), 1)


class SetDHCPSyncScheduleJobTests(TestCase):
    """SetDHCPSyncScheduleJob.run() dispatches to the right DHCPSyncJob call
    depending on the recurring/sync_at kwargs it was enqueued with."""

    def _make_scheduler_job(self):
        return Job.objects.create(
            name=SetDHCPSyncScheduleJob.name, status=JobStatusChoices.STATUS_RUNNING,
            job_id=uuid.uuid4(), queue_name='default',
        )

    @mock.patch('netbox_windows_dhcp.background_tasks.DHCPSyncJob.enqueue')
    def test_recurring_false_enqueues_one_off(self, mock_enqueue):
        mock_enqueue.return_value = _make_sync_job()

        SetDHCPSyncScheduleJob(self._make_scheduler_job()).run(recurring=False)

        mock_enqueue.assert_called_once_with(interval=None)

    @mock.patch('netbox_windows_dhcp.background_tasks.DHCPSyncJob.converge_schedule')
    def test_recurring_true_converges_with_sync_at_and_live_interval(self, mock_converge):
        mock_converge.return_value = _make_sync_job()
        set_plugin_settings(sync_interval=30)
        sync_at = timezone.now() + timedelta(hours=1)

        SetDHCPSyncScheduleJob(self._make_scheduler_job()).run(recurring=True, sync_at=sync_at)

        mock_converge.assert_called_once_with(schedule_at=sync_at, interval=30)


class ReservationFailureJobStatusTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        set_plugin_settings(sync_ip_addresses=True, push_reservations=True, push_scope_info=False)
        cls.server = make_server(name='Dev 1')
        make_scope(name='Building A', prefix=make_prefix('10.0.1.0/24'), server=cls.server,
                   router='10.0.1.1')

    def _fake(self, results):
        return FakePSUClient(
            scopes=[dict(FAKE_SCOPE_SNAKE)], leases={SCOPE_ID: []}, reservations={SCOPE_ID: []},
            exclusions={SCOPE_ID: []}, scope_options={SCOPE_ID: []}, reservation_results=results,
        )

    def _run(self, job_class, fake, **kwargs):
        reserved_ip('10.0.1.100')
        job = make_job(job_class.name)
        with mock.patch(PSU_CLIENT, return_value=fake), mock.patch(BY_IP, return_value=True):
            job_class(job).run(**kwargs)
        return job

    def test_server_sync_job_fails_with_a_summary(self):
        fake = self._fake({'10.0.1.100': {'status': 'error', 'error': 'boom'}})
        with self.assertRaises(JobFailed):
            self._run(DHCPServerSyncJob, fake, server_pk=self.server.pk)
        job = Job.objects.get(name=DHCPServerSyncJob.name)
        self.assertEqual(job.error, 'Dev 1: 1 reservation change(s) failed — see log')

    def test_server_sync_job_completes_when_nothing_failed(self):
        self._run(DHCPServerSyncJob, self._fake({}), server_pk=self.server.pk)  # must not raise

    def test_scheduled_sync_job_fails_with_a_summary(self):
        fake = self._fake({'10.0.1.100': {'status': 'error', 'error': 'boom'}})
        with self.assertRaises(JobFailed):
            self._run(DHCPSyncJob, fake)
        job = Job.objects.get(name=DHCPSyncJob.name)
        self.assertIn('Dev 1: 1 reservation change(s) failed — see log', job.error)
        self.assertEqual(DHCPServer.objects.get(pk=self.server.pk).health_status, DHCPServer.HEALTH_HEALTHY)


class DHCPPSUUpdateJobFailureVisibilityTests(TestCase):
    """
    Regression coverage for a bug reproduced manually against a real read-only
    server: every PSU call failed with 403, yet the job reported Completed
    instead of Failed. DHCPPSUUpdateJob must now raise JobFailed on any real
    failure — a hard-stop (fetch/parse/restart) or a partial per-item failure.
    """

    def test_fetch_endpoints_failure_raises_job_failed(self):
        server = make_server()
        client = mock.Mock()
        client.get_dhcp_endpoints.side_effect = PSUClientError('forbidden', status_code=403)
        job = make_job(DHCPPSUUpdateJob.name)

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=client):
            with self.assertRaises(JobFailed):
                DHCPPSUUpdateJob(job).run(server_pk=server.pk)

        job.refresh_from_db()
        self.assertIn('forbidden', job.error)

    def test_partial_update_failure_still_raises_job_failed(self):
        server = make_server()
        client = mock.Mock()
        client.get_dhcp_endpoints.return_value = [
            {'id': 1, 'url': '/api/dhcp/health', 'method': 'GET'},
        ]
        client.update_endpoint.side_effect = PSUClientError('forbidden', status_code=403)
        client.create_endpoint.return_value = {}
        client.restart_endpoints.return_value = None
        client.ping_read.return_value = {'version': '1.1.2'}
        job = make_job(DHCPPSUUpdateJob.name)

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=client):
            with mock.patch('time.sleep'):
                with self.assertRaises(JobFailed):
                    DHCPPSUUpdateJob(job).run(server_pk=server.pk)

        job.refresh_from_db()
        self.assertIn('forbidden', job.error)
        # Endpoints that didn't fail should still have been attempted.
        client.restart_endpoints.assert_called_once()

    def test_full_success_does_not_raise(self):
        server = make_server()
        client = mock.Mock()
        client.get_dhcp_endpoints.return_value = []
        client.create_endpoint.return_value = {}
        client.restart_endpoints.return_value = None
        client.ping_read.return_value = {'version': '1.1.2'}
        job = make_job(DHCPPSUUpdateJob.name)

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=client):
            with mock.patch('time.sleep'):
                DHCPPSUUpdateJob(job).run(server_pk=server.pk)  # must not raise

        job.refresh_from_db()
        self.assertEqual(job.error, '')


class DHCPScopePushJobAccessLevelTests(TestCase):
    """DHCPScopePushJob must skip entirely (no pull component) against a
    known-read-only server, rather than attempting the push and reacting to
    a 403 — mirrors _sync_server's proactive skip."""

    def test_skips_push_when_server_is_read_only(self):
        server = make_server()
        server.access_level = DHCPServer.ACCESS_RO
        server.save(update_fields=['access_level'])
        prefix = make_prefix('10.0.1.0/24')
        scope = make_scope(name='A', prefix=prefix, server=server, router='10.0.1.1')
        fake = FakePSUClient(scopes=[], ping_write_error=PSUClientError('forbidden', status_code=403))
        job = make_job(DHCPScopePushJob.name)

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            DHCPScopePushJob(job).run(server_pk=server.pk, scope_pks=[scope.pk])

        self.assertEqual(fake.created_scopes, [])
        self.assertEqual(fake.updated_scopes, [])
        server.refresh_from_db()
        self.assertEqual(server.access_level, DHCPServer.ACCESS_RO)


class DHCPScopeDeleteJobAccessLevelTests(TestCase):
    """DHCPScopeDeleteJob must likewise skip entirely against a known-read-only
    server rather than attempting the delete."""

    def test_skips_delete_when_server_is_read_only(self):
        server = make_server()
        server.access_level = DHCPServer.ACCESS_RO
        server.save(update_fields=['access_level'])
        fake = FakePSUClient(ping_write_error=PSUClientError('forbidden', status_code=403))
        job = make_job(DHCPScopeDeleteJob.name)
        deletes = [{'scope_id': '10.0.1.0', 'scope_name': 'A', 'failover_name': None, 'maintenance_mode': False}]

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            DHCPScopeDeleteJob(job).run(server_pk=server.pk, deletes=deletes)

        self.assertEqual(fake.deleted_scopes, [])
        server.refresh_from_db()
        self.assertEqual(server.access_level, DHCPServer.ACCESS_RO)
