"""
UI view tests using NetBox's ViewTestCases harness.

Mixins are composed per model to match exactly the views that are registered
(see urls.py / views.py); notably:
  * No model registers a bulk-import (CSV) view, so those mixins are omitted.
  * DHCPExclusionRange has no list or bulk views.
  * DHCPFailover's "add" view is intentionally a redirect (failovers are
    import-only) — Create is replaced by a redirect assertion.
  * DHCPScope's add/edit/delete/bulk views are gated behind the push_scope_info
    setting, so those tests enable it and patch the resulting job enqueue.

Custom action views (sync / maintenance / global sync) are covered separately
with the job layer patched, so no test reaches RQ/Redis or a DHCP server.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from ipam.models import Prefix
from utilities.testing import TestCase, ViewTestCases

from ..models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPPluginSettings,
    DHCPScope,
    DHCPServer,
)
from .base import (
    PluginViewTestMixin,
    clear_builtin_option_codes,
    make_failover,
    make_server,
    set_plugin_settings,
)

ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPServerSyncJob.enqueue'
ENQUEUE_ONCE = 'netbox_windows_dhcp.background_tasks.DHCPSyncJob.enqueue_once'
SYNC_ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPSyncJob.enqueue'
IMPORT_ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPImportJob.enqueue'
PSU_ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPPSUUpdateJob.enqueue'


def _job_mock():
    """A stand-in job whose get_absolute_url() returns a real string for redirect()."""
    job = mock.Mock()
    job.get_absolute_url.return_value = '/core/jobs/1/'
    return job


class DHCPServerViewTests(
    PluginViewTestMixin,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = DHCPServer

    @classmethod
    def setUpTestData(cls):
        DHCPServer.objects.bulk_create([
            DHCPServer(name='Server 1', hostname='s1.example.com'),
            DHCPServer(name='Server 2', hostname='s2.example.com'),
            DHCPServer(name='Server 3', hostname='s3.example.com'),
        ])
        cls.form_data = {
            'name': 'Server X', 'hostname': 'serverx.example.com', 'port': 443,
            'use_https': True, 'verify_ssl': True, 'sync_standalone_scopes': True,
        }


class DHCPFailoverViewTests(
    PluginViewTestMixin,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = DHCPFailover

    @classmethod
    def setUpTestData(cls):
        servers = DHCPServer.objects.bulk_create([
            DHCPServer(name=f'FoSrv {i}', hostname=f'fo{i}.example.com') for i in range(1, 7)
        ])
        DHCPFailover.objects.bulk_create([
            DHCPFailover(name='FO 1', primary_server=servers[0], secondary_server=servers[1]),
            DHCPFailover(name='FO 2', primary_server=servers[2], secondary_server=servers[3]),
            DHCPFailover(name='FO 3', primary_server=servers[4], secondary_server=servers[5]),
        ])
        cls.form_data = {
            'name': 'FO X',
            'primary_server': servers[0].pk,
            'secondary_server': servers[1].pk,
            'mode': 'LoadBalance',
            'max_client_lead_time': 3600,
            'max_response_delay': 30,
            'sync_enabled': True,
            'enable_auth': False,
        }

    def test_add_view_is_readonly_redirect(self):
        """The failover 'add' view redirects without creating (import-only)."""
        self.add_permissions('netbox_windows_dhcp.add_dhcpfailover')
        url = reverse('plugins:netbox_windows_dhcp:dhcpfailover_add')
        before = DHCPFailover.objects.count()
        response = self.client.get(url)
        self.assertHttpStatus(response, 302)
        self.assertEqual(DHCPFailover.objects.count(), before)


class DHCPOptionCodeDefinitionViewTests(
    PluginViewTestMixin,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = DHCPOptionCodeDefinition

    @classmethod
    def setUpTestData(cls):
        # Clear migration-seeded built-ins so delete/bulk-delete target only our
        # non-builtin rows (the is_builtin guard would 500 the delete otherwise).
        clear_builtin_option_codes()
        DHCPOptionCodeDefinition.objects.bulk_create([
            DHCPOptionCodeDefinition(code=240, name='Opt 240'),
            DHCPOptionCodeDefinition(code=241, name='Opt 241'),
            DHCPOptionCodeDefinition(code=242, name='Opt 242'),
        ])
        cls.form_data = {
            'code': 250, 'name': 'Opt 250', 'data_type': 'String',
            'description': '', 'vendor_class': '',
        }


class DHCPOptionValueViewTests(
    PluginViewTestMixin,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = DHCPOptionValue

    @classmethod
    def setUpTestData(cls):
        opt = DHCPOptionCodeDefinition.objects.create(code=200, name='DNS')
        DHCPOptionValue.objects.bulk_create([
            DHCPOptionValue(option_definition=opt, value='10.0.0.1', friendly_name='V1'),
            DHCPOptionValue(option_definition=opt, value='10.0.0.2', friendly_name='V2'),
            DHCPOptionValue(option_definition=opt, value='10.0.0.3', friendly_name='V3'),
        ])
        cls.form_data = {
            'option_definition': opt.pk, 'value': '10.9.9.9', 'friendly_name': 'VX',
        }


class DHCPScopeViewTests(
    PluginViewTestMixin,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkEditObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = DHCPScope

    @classmethod
    def setUpTestData(cls):
        prefix = Prefix.objects.create(prefix='10.0.1.0/24', status='active')
        server = DHCPServer.objects.create(name='ScopeSrv', hostname='scopesrv.example.com')
        # Create scopes while push_scope_info is still False so the post_save
        # signal does not enqueue during fixture setup.
        DHCPScope.objects.bulk_create([
            DHCPScope(name='Scope 1', prefix=prefix, server=server, start_ip='10.0.1.10', end_ip='10.0.1.20'),
            DHCPScope(name='Scope 2', prefix=prefix, server=server, start_ip='10.0.1.30', end_ip='10.0.1.40'),
            DHCPScope(name='Scope 3', prefix=prefix, server=server, start_ip='10.0.1.50', end_ip='10.0.1.60'),
        ])
        # Unblock the gated add/edit/delete/bulk views.
        set_plugin_settings(push_scope_info=True)
        cls.form_data = {
            'name': 'Scope X', 'prefix': prefix.pk,
            'start_ip': '10.0.1.150', 'end_ip': '10.0.1.200', 'router': '10.0.1.1',
            'server': server.pk, 'lease_lifetime_value': 1, 'lease_lifetime_unit': 'days',
        }
        cls.bulk_edit_data = {'router': '10.0.1.254'}

    def setUp(self):
        super().setUp()
        # Saving a scope with push_scope_info on fires a signal that enqueues a
        # server-sync job. Patch it so no test reaches RQ.
        patcher = mock.patch(ENQUEUE)
        patcher.start()
        self.addCleanup(patcher.stop)


class DHCPExclusionRangeViewTests(
    PluginViewTestMixin,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
):
    # No bulk views are registered for exclusion ranges.
    model = DHCPExclusionRange

    @classmethod
    def setUpTestData(cls):
        prefix = Prefix.objects.create(prefix='10.0.1.0/24', status='active')
        server = DHCPServer.objects.create(name='ExSrv', hostname='exsrv.example.com')
        scope = DHCPScope.objects.create(
            name='ExScope', prefix=prefix, server=server, start_ip='10.0.1.10', end_ip='10.0.1.254',
        )
        DHCPExclusionRange.objects.bulk_create([
            DHCPExclusionRange(scope=scope, start_ip='10.0.1.20', end_ip='10.0.1.25'),
            DHCPExclusionRange(scope=scope, start_ip='10.0.1.30', end_ip='10.0.1.35'),
            DHCPExclusionRange(scope=scope, start_ip='10.0.1.40', end_ip='10.0.1.45'),
        ])
        cls.form_data = {
            'scope': scope.pk, 'start_ip': '10.0.1.210', 'end_ip': '10.0.1.220',
        }


class CustomActionViewTests(TestCase):
    """Sync / maintenance / global-sync action views, with the job layer patched."""

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        self.server = make_server()

    def test_server_sync_enqueues_job(self):
        job_mock = mock.Mock()
        job_mock.get_absolute_url.return_value = '/core/jobs/1/'
        url = reverse('plugins:netbox_windows_dhcp:dhcpserver_sync', kwargs={'pk': self.server.pk})
        with mock.patch(ENQUEUE, return_value=job_mock) as enq:
            self.client.post(url)
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs.get('server_pk'), self.server.pk)

    def test_global_sync_enqueues_all_servers(self):
        make_server(name='Server 2', hostname='s2.example.com')
        url = reverse('plugins:netbox_windows_dhcp:global_sync')
        with mock.patch(ENQUEUE, return_value=mock.Mock()) as enq:
            self.client.post(url)
        self.assertEqual(enq.call_count, 2)

    def test_server_maintenance_enable(self):
        url = reverse('plugins:netbox_windows_dhcp:dhcpserver_maintenance', kwargs={'pk': self.server.pk})
        self.client.post(url, {'maintenance_mode': '1', 'maintenance_notes': 'planned'})
        self.server.refresh_from_db()
        self.assertTrue(self.server.maintenance_mode)

    def test_server_maintenance_disable(self):
        self.server.maintenance_mode = True
        self.server.save()
        url = reverse('plugins:netbox_windows_dhcp:dhcpserver_maintenance', kwargs={'pk': self.server.pk})
        self.client.post(url, {'maintenance_notes': ''})  # no maintenance_mode=1 → disable
        self.server.refresh_from_db()
        self.assertFalse(self.server.maintenance_mode)

    def test_server_import_enqueues_job(self):
        url = reverse('plugins:netbox_windows_dhcp:dhcpserver_import', kwargs={'pk': self.server.pk})
        with mock.patch(IMPORT_ENQUEUE, return_value=_job_mock()) as enq:
            self.client.post(url)
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs.get('server_pk'), self.server.pk)

    def test_server_psu_update_enqueues_job(self):
        url = reverse('plugins:netbox_windows_dhcp:dhcpserver_psu_update', kwargs={'pk': self.server.pk})
        with mock.patch(PSU_ENQUEUE, return_value=_job_mock()) as enq:
            self.client.post(url)
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs.get('server_pk'), self.server.pk)

    def test_failover_toggle_sync_flips_flag(self):
        failover = make_failover()  # sync_enabled defaults to True
        url = reverse('plugins:netbox_windows_dhcp:dhcpfailover_toggle_sync', kwargs={'pk': failover.pk})
        self.client.post(url)
        failover.refresh_from_db()
        self.assertFalse(failover.sync_enabled)


class SettingsViewTests(TestCase):
    """SettingsView (superuser-gated + persists) and ScheduleSyncView."""

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()

    def test_settings_get_superuser_ok(self):
        with mock.patch(ENQUEUE_ONCE):
            response = self.client.get(reverse('plugins:netbox_windows_dhcp:settings'))
        self.assertHttpStatus(response, 200)

    def test_settings_get_non_superuser_redirects(self):
        plain = get_user_model().objects.create_user('plain', password='x')
        c = Client()
        c.force_login(plain)
        response = c.get(reverse('plugins:netbox_windows_dhcp:settings'))
        self.assertHttpStatus(response, 302)

    def test_settings_post_persists(self):
        # Use standard IP statuses (the form's choices come from IPAddressStatusChoices).
        data = {
            'lease_status': 'active',
            'reservation_status': 'reserved',
            'sync_interval': 120,
            'sync_queue': 'default',
            'sync_job_timeout': 300,
        }
        with mock.patch(ENQUEUE_ONCE):
            response = self.client.post(reverse('plugins:netbox_windows_dhcp:settings'), data)
        self.assertHttpStatus(response, 302)
        self.assertEqual(DHCPPluginSettings.load().sync_interval, 120)

    def test_schedule_run_now_enqueues_immediately(self):
        # Patch enqueue_once too: the settings singleton's post_save reschedules the
        # recurring chain via enqueue_once, which itself calls enqueue.
        with mock.patch(ENQUEUE_ONCE), \
                mock.patch(SYNC_ENQUEUE, return_value=_job_mock()) as enq:
            self.client.post(reverse('plugins:netbox_windows_dhcp:schedule_sync'), {'action': 'run_now'})
        enq.assert_called_once()
        self.assertIsNone(enq.call_args.kwargs.get('interval'))

    def test_schedule_future_reschedules(self):
        start_at = (timezone.now() + timedelta(days=1)).replace(microsecond=0)
        with mock.patch(ENQUEUE_ONCE) as enq_once:
            self.client.post(reverse('plugins:netbox_windows_dhcp:schedule_sync'), {
                'action': 'schedule', 'start_at': start_at.isoformat(),
            })
        self.assertTrue(enq_once.called)
        self.assertIn('schedule_at', enq_once.call_args.kwargs)
