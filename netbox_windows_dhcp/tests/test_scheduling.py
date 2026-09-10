"""
Regression tests for the duplicate/multiplying "Windows DHCP Sync" job chain
bug (v1.3.2/v1.3.3, silently reverted in v1.3.4 c3d7d45, hit again in
production in v1.3.6 — a 60-minute sync interval accumulated to 33 concurrent
overlapping sync jobs). If DHCPSyncJob.run() or converge_schedule() stop
pruning duplicate scheduled/pending jobs, these tests fail.

Job.delete() cancels the underlying RQ/Redis entry via
django_rq.get_queue(...).fetch_job(...), so django_rq.get_queue is mocked
rather than the RQ layer being exercised for real.
"""

import uuid
from unittest import mock

from django.test import TestCase

from core.choices import JobStatusChoices
from core.models import Job

from ..background_tasks import DHCPSyncJob
from .base import set_plugin_settings

GET_QUEUE = 'core.models.jobs.django_rq.get_queue'


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
        current = _make_sync_job(status=JobStatusChoices.STATUS_RUNNING)

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
        current = _make_sync_job(status=JobStatusChoices.STATUS_RUNNING)
        other_running = _make_sync_job(status=JobStatusChoices.STATUS_RUNNING)

        DHCPSyncJob(current).run()

        self.assertTrue(Job.objects.filter(pk=other_running.pk).exists())

    @mock.patch(GET_QUEUE)
    def test_run_with_no_duplicates_is_a_noop(self, mock_get_queue):
        mock_get_queue.return_value = mock.MagicMock()
        current = _make_sync_job(status=JobStatusChoices.STATUS_RUNNING)

        DHCPSyncJob(current).run()

        self.assertTrue(Job.objects.filter(pk=current.pk).exists())


class ConvergeScheduleTests(TestCase):
    """DHCPSyncJob.converge_schedule() — used by the settings-save signal and
    the Schedule button to collapse duplicates immediately, without waiting
    for the next run() cycle."""

    @mock.patch(GET_QUEUE)
    def test_converge_schedule_collapses_duplicates_and_keeps_earliest(self, mock_get_queue):
        mock_get_queue.return_value = mock.MagicMock()

        from django.utils import timezone
        from datetime import timedelta

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

    @mock.patch(GET_QUEUE)
    @mock.patch('netbox_windows_dhcp.background_tasks.DHCPSyncJob.enqueue_once')
    def test_converge_schedule_applies_kwargs_via_enqueue_once(self, mock_enqueue_once, mock_get_queue):
        mock_get_queue.return_value = mock.MagicMock()

        DHCPSyncJob.converge_schedule(interval=120)

        mock_enqueue_once.assert_called_once_with(interval=120)
