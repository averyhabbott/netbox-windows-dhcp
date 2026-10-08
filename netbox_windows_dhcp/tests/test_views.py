"""
UI view tests using NetBox's ViewTestCases harness.

Mixins are composed per model to match exactly the views that are registered
(see urls.py / views.py); notably:
  * No model registers a bulk-import (CSV) view, so those mixins are omitted.
  * DHCPExclusionRange's only bulk view is bulk delete.
  * DHCPFailover's "add" view is intentionally a redirect (failovers are
    import-only) — Create is replaced by a redirect assertion.
  * DHCPScope, DHCPExclusionRange and DHCPOptionValue write views are gated
    behind the push_scope_info setting, so those tests enable it (scope tests
    also patch the resulting job enqueue).

Custom action views (sync / maintenance / global sync) are covered separately
with the job layer patched, so no test reaches RQ/Redis or a DHCP server.
"""

from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from ipam.models import IPAddress, Prefix, VRF
from tenancy.models import Tenant
from utilities.testing import TestCase, ViewTestCases

from ..models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPLeaseInfo,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPPluginSettings,
    DHCPScope,
    DHCPServer,
)
from .base import (
    changes_for,
    grant,
    PluginViewTestMixin,
    clear_builtin_option_codes,
    job_mock,
    make_failover,
    make_option_definition,
    make_option_value,
    make_prefix,
    make_scope,
    make_server,
    make_unassigned_scope,
    set_plugin_settings,
    ui_url,
)


ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPServerSyncJob.enqueue'
SCHEDULE_JOB_ENQUEUE = 'netbox_windows_dhcp.background_tasks.SetDHCPSyncScheduleJob.enqueue'
IMPORT_ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPImportJob.enqueue'
PSU_ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPPSUUpdateJob.enqueue'
SETTINGS_FORM = {
    'lease_status': 'active',
    'reservation_status': 'reserved',
    'sync_interval': 60,
    'sync_queue': 'default',
    'sync_job_timeout': 300,
    'sync_log_level': 'DEBUG',
}
SCOPE_PUSH = 'netbox_windows_dhcp.signals._queue_scope_push'


class _OptionCodeFixtures:

    def _fixtures(self):
        set_plugin_settings(push_scope_info=True)
        code = make_option_definition(code=210, name='Opt 210')
        other = make_option_definition(code=211, name='Opt 211')
        self.a = make_option_value(option_definition=code, value='1.1.1.1', friendly_name='A')
        self.b = make_option_value(option_definition=code, value='2.2.2.2', friendly_name='B')
        self.c = make_option_value(option_definition=other, value='3.3.3.3', friendly_name='C')
        self.scope = make_scope()


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
        # Only the NetBox-side fields can be edited (the rest are read-only on the form).
        cls.form_data = {
            'description': 'Edited failover',
            'default_scope_vrf': VRF.objects.create(name='Red').pk,
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
        # Unblock the gated add/edit/delete/bulk views.
        set_plugin_settings(push_scope_info=True)


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
        # One scope per prefix, and one per network on a server.
        prefixes = [Prefix.objects.create(prefix=f'10.0.{i}.0/24', status='active') for i in range(1, 4)]
        new_prefix = Prefix.objects.create(prefix='10.0.9.0/24', status='active')
        server = DHCPServer.objects.create(name='ScopeSrv', hostname='scopesrv.example.com')
        # Create scopes while push_scope_info is still False so the post_save
        # signal does not enqueue during fixture setup.
        DHCPScope.objects.bulk_create([
            DHCPScope(name=f'Scope {i}', prefix=prefixes[i - 1], network=f'10.0.{i}.0', prefix_length=24,
                      server=server, start_ip=f'10.0.{i}.10', end_ip=f'10.0.{i}.20')
            for i in range(1, 4)
        ])
        # Unblock the gated add/edit/delete/bulk views.
        set_plugin_settings(push_scope_info=True)
        cls.form_data = {
            'name': 'Scope X', 'description': 'Building X', 'prefix': new_prefix.pk,
            'start_ip': '10.0.9.150', 'end_ip': '10.0.9.200', 'router': '10.0.9.1',
            'server': server.pk, 'lease_lifetime_value': 1, 'lease_lifetime_unit': 'days',
        }
        cls.bulk_edit_data = {'router': '10.0.1.254', 'description': 'Bulk described'}

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
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    # Bulk delete is the only bulk view registered for exclusion ranges.
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
            'description': 'Printers',
        }
        # Unblock the gated add/edit/delete views.
        set_plugin_settings(push_scope_info=True)


class ScopeWithoutPrefixViewTests(TestCase):

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        self.server = make_server()

    def test_detail_page_renders(self):
        scope = make_unassigned_scope(server=self.server)
        response = self.client.get(scope.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Unassigned')
        self.assertContains(response, '10.0.1.0/24')

    def test_create_through_the_form(self):
        set_plugin_settings(push_scope_info=True)
        data = {
            'name': 'Form scope', 'network': '10.0.3.0', 'prefix_length': 24,
            'start_ip': '10.0.3.10', 'end_ip': '10.0.3.20', 'server': self.server.pk,
            'lease_lifetime_value': 1, 'lease_lifetime_unit': 'days',
        }
        with mock.patch('netbox_windows_dhcp.background_tasks.DHCPScopePushJob.enqueue'):
            response = self.client.post(reverse('plugins:netbox_windows_dhcp:dhcpscope_add'), data)
        self.assertHttpStatus(response, 302)
        scope = DHCPScope.objects.get(name='Form scope')
        self.assertIsNone(scope.prefix)
        self.assertEqual((scope.network, scope.prefix_length), ('10.0.3.0', 24))

    def test_a_prefix_is_not_required_but_something_is(self):
        set_plugin_settings(push_scope_info=True)
        data = {
            'name': 'Form scope', 'start_ip': '10.0.3.10', 'end_ip': '10.0.3.20',
            'server': self.server.pk, 'lease_lifetime_value': 1, 'lease_lifetime_unit': 'days',
        }
        response = self.client.post(reverse('plugins:netbox_windows_dhcp:dhcpscope_add'), data)
        self.assertHttpStatus(response, 200)
        self.assertTrue(response.context['form'].errors)
        self.assertFalse(DHCPScope.objects.filter(name='Form scope').exists())


class ScopeDetailVRFTests(TestCase):

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()

    def test_ip_table_only_lists_the_scopes_vrf(self):
        red = VRF.objects.create(name='Red')
        scope = make_scope(prefix=make_prefix('10.0.1.0/24', vrf=red))
        IPAddress.objects.create(address='10.0.1.50/24', status='dhcp', vrf=red)
        IPAddress.objects.create(address='10.0.1.51/24', status='dhcp')
        response = self.client.get(reverse('plugins:netbox_windows_dhcp:dhcpscope', args=[scope.pk]))
        self.assertHttpStatus(response, 200)
        listed = [str(ip.address) for ip in response.context['ip_table'].data]
        self.assertEqual(listed, ['10.0.1.50/24'])


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
        with mock.patch(IMPORT_ENQUEUE, return_value=job_mock()) as enq:
            self.client.post(url)
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs.get('server_pk'), self.server.pk)

    def test_server_psu_update_enqueues_job(self):
        url = reverse('plugins:netbox_windows_dhcp:dhcpserver_psu_update', kwargs={'pk': self.server.pk})
        with mock.patch(PSU_ENQUEUE, return_value=job_mock()) as enq:
            self.client.post(url)
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs.get('server_pk'), self.server.pk)

    def test_failover_toggle_sync_flips_flag(self):
        failover = make_failover()  # sync_enabled defaults to True
        url = reverse('plugins:netbox_windows_dhcp:dhcpfailover_toggle_sync', kwargs={'pk': failover.pk})
        self.client.post(url)
        failover.refresh_from_db()
        self.assertFalse(failover.sync_enabled)


class SyncNowNeedsPostTests(TestCase):

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        self.server = make_server()
        self.url = reverse('plugins:netbox_windows_dhcp:dhcpserver_sync', kwargs={'pk': self.server.pk})

    def test_get_is_refused_and_starts_nothing(self):
        with mock.patch(ENQUEUE) as enq:
            response = self.client.get(self.url)
        self.assertHttpStatus(response, 405)
        enq.assert_not_called()


class PerObjectPermissionViewTests(TestCase):
    """The user may change server A only (and view both)."""

    def setUp(self):
        super().setUp()
        self.a = make_server(name='A', hostname='a.example.com')
        self.b = make_server(name='B', hostname='b.example.com')
        grant(self.user, DHCPServer, ['view'])
        grant(self.user, DHCPServer, ['change'], pk=self.a.pk)

    def test_single_sync_only_on_permitted_server(self):
        with mock.patch(ENQUEUE, return_value=job_mock()) as enq:
            self.assertEqual(self.client.post(ui_url('dhcpserver_sync', self.b.pk)).status_code, 404)
            enq.assert_not_called()
            self.client.post(ui_url('dhcpserver_sync', self.a.pk))
            enq.assert_called_once()

    def test_global_sync_skips_unpermitted(self):
        with mock.patch(ENQUEUE, return_value=job_mock()) as enq:
            self.client.post(ui_url('global_sync'))
        self.assertEqual([c.kwargs['server_pk'] for c in enq.call_args_list], [self.a.pk])

    def test_bulk_maintenance_skips_unpermitted(self):
        self.client.post(ui_url('dhcpserver_bulk_maintenance'),
                         {'pk': [self.a.pk, self.b.pk], 'confirm': '1', 'maintenance_mode': '1'})
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual((self.a.maintenance_mode, self.b.maintenance_mode), (True, False))

    def test_bulk_psu_update_skips_unpermitted(self):
        with mock.patch(PSU_ENQUEUE, return_value=job_mock()) as enq:
            self.client.post(ui_url('dhcpserver_bulk_psu_update'), {'pk': [self.a.pk, self.b.pk]})
        self.assertEqual([c.kwargs['server_pk'] for c in enq.call_args_list], [self.a.pk])

    def test_single_maintenance_and_import_and_cert_remove_refused_on_other_server(self):
        DHCPServer.objects.filter(pk=self.b.pk).update(ca_cert='pem')
        for name in ('dhcpserver_maintenance', 'dhcpserver_import', 'dhcpserver_certremove',
                     'dhcpserver_psu_update'):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(ui_url(name, self.b.pk), {'maintenance_mode': '1'}).status_code,
                                 404)
        self.b.refresh_from_db()
        self.assertEqual((self.b.maintenance_mode, self.b.ca_cert), (False, 'pem'))

    def test_test_connection_needs_change_on_that_server(self):
        response = self.client.post(ui_url('dhcpserver_test_connection', self.b.pk), {'hostname': 'b'})
        self.assertEqual(response.status_code, 403)

    def test_failover_bulk_toggle_skips_unpermitted(self):
        fo_a = make_failover(name='FA', primary=self.a, secondary=make_server(name='A2', hostname='a2'))
        fo_b = make_failover(name='FB', primary=self.b, secondary=make_server(name='B2', hostname='b2'))
        grant(self.user, DHCPFailover, ['change'], pk=fo_a.pk)
        self.client.post(reverse('plugins:netbox_windows_dhcp:dhcpfailover_bulk_toggle_sync'),
                         {'pk': [fo_a.pk, fo_b.pk]})
        fo_a.refresh_from_db()
        fo_b.refresh_from_db()
        self.assertEqual((fo_a.sync_enabled, fo_b.sync_enabled), (False, True))


class UnprivilegedAJAXTests(TestCase):

    def test_cert_fetch_needs_add_or_change_on_servers(self):
        with mock.patch('netbox_windows_dhcp.cert_utils.fetch_cert_info') as fetch:
            response = self.client.post(reverse('plugins:netbox_windows_dhcp:dhcpserver_cert_fetch'),
                                        {'hostname': 'x.example.com', 'port': '443'})
        self.assertEqual(response.status_code, 403)
        fetch.assert_not_called()

    def test_test_connection_new_needs_add_or_change(self):
        response = self.client.post(reverse('plugins:netbox_windows_dhcp:dhcpserver_test_connection_new'),
                                    {'hostname': 'x.example.com'})
        self.assertEqual(response.status_code, 403)


class CertFetchAllowlistTests(TestCase):
    """With restrict_allowlist on, Fetch Certificate only connects to hostnames marked allowed."""

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()

    def _fetch(self, hostname, plugin_config):
        plugins_config = {**settings.PLUGINS_CONFIG, 'netbox_windows_dhcp': plugin_config}
        cert = {'pem': 'PEM', 'not_after': None}
        with override_settings(PLUGINS_CONFIG=plugins_config), \
                mock.patch('netbox_windows_dhcp.cert_utils.fetch_cert_info', return_value=cert) as fetch:
            response = self.client.post(reverse('plugins:netbox_windows_dhcp:dhcpserver_cert_fetch'),
                                        {'hostname': hostname, 'port': '443'})
        return response.json()['ok'], fetch.called

    def test_which_hostnames_are_fetched(self):
        restricted = {'restrict_allowlist': True,
                      'server_overrides': {'dhcp01.example.com': {'allowed': True},
                                           'dhcp02.example.com': {'api_key': 'x'}}}
        cases = (
            ('allowlist on, listed and allowed', 'dhcp01.example.com', restricted, True),
            ('allowlist on, listed without allowed', 'dhcp02.example.com', restricted, False),
            ('allowlist on, not listed', 'other.example.com', restricted, False),
            ('allowlist off', 'other.example.com', {}, True),
        )
        for label, hostname, plugin_config, fetched in cases:
            with self.subTest(label):
                self.assertEqual(self._fetch(hostname, plugin_config), (fetched, fetched))


class CurrentMaintenanceViewTests(TestCase):

    def setUp(self):
        super().setUp()
        self.a = make_server(name='A', hostname='a.example.com', maintenance_mode=True)
        self.b = make_server(name='B', hostname='b.example.com', maintenance_mode=True)
        grant(self.user, DHCPServer, ['view'], pk=self.a.pk)

    def test_lists_only_viewable_items(self):
        response = self.client.get(reverse('plugins:netbox_windows_dhcp:current_maintenance'))
        self.assertEqual([i['object'] for i in response.context['items']], [self.a])

    def test_bulk_disable_only_changes_permitted(self):
        grant(self.user, DHCPServer, ['change'], pk=self.a.pk)
        self.client.post(reverse('plugins:netbox_windows_dhcp:current_maintenance_bulk_disable'),
                         {'selected': [f'server:{self.a.pk}', f'server:{self.b.pk}']})
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual((self.a.maintenance_mode, self.b.maintenance_mode), (False, True))


class DuplicateOptionCodeBulkEditTests(_OptionCodeFixtures, TestCase):

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        self._fixtures()
        self.scope.option_values.add(self.a)
        self.other_scope = make_scope(name='Scope 2', server=self.scope.server, prefix=make_prefix('10.0.2.0/24'),
                                      start_ip='10.0.2.10', end_ip='10.0.2.254')

    def _bulk_edit(self, **data):
        with mock.patch(SCOPE_PUSH):
            return self.client.post(reverse('plugins:netbox_windows_dhcp:dhcpscope_bulk_edit'), {
                'pk': [self.scope.pk, self.other_scope.pk], '_apply': '1', 'description': 'bulk', **data,
            })

    def test_bulk_add_refuses_whole_edit_and_saves_nothing(self):
        self._bulk_edit(add_option_values=[self.b.pk])
        self.assertEqual(list(self.scope.option_values.all()), [self.a])
        self.assertEqual(self.other_scope.option_values.count(), 0)
        self.scope.refresh_from_db()
        self.assertNotEqual(self.scope.description, 'bulk')

    def test_bulk_add_with_matching_remove_is_allowed(self):
        self._bulk_edit(add_option_values=[self.b.pk], remove_option_values=[self.a.pk])
        self.assertEqual(list(self.scope.option_values.all()), [self.b])
        self.assertEqual(list(self.other_scope.option_values.all()), [self.b])


class SettingsViewTests(TestCase):
    """SettingsView (superuser-gated + persists) and ScheduleSyncView."""

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()

    def test_settings_page_shows_every_setting_once_with_detail_popups(self):
        from ..forms import _SETTINGS_HELP_DETAILS, PluginSettingsForm

        response = self.client.get(reverse('plugins:netbox_windows_dhcp:settings'))
        content = response.content.decode()
        for name in PluginSettingsForm.Meta.fields:
            self.assertEqual(content.count(f'id="id_{name}"'), 1, name)
        for name in _SETTINGS_HELP_DETAILS:
            self.assertIn(f'data-bs-target="#{name}_details"', content)
            self.assertIn(f'id="{name}_details"', content)

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
            'sync_log_level': 'DEBUG',
        }
        response = self.client.post(reverse('plugins:netbox_windows_dhcp:settings'), data)
        self.assertHttpStatus(response, 302)
        self.assertEqual(DHCPPluginSettings.load().sync_interval, 120)

    def test_reservation_placeholders_off_by_default_and_saves(self):
        self.assertFalse(DHCPPluginSettings.load().reservation_placeholders)
        data = {
            'lease_status': 'active',
            'reservation_status': 'reserved',
            'sync_interval': 60,
            'sync_queue': 'default',
            'sync_job_timeout': 300,
            'sync_log_level': 'DEBUG',
            'reservation_placeholders': 'on',
        }
        response = self.client.post(reverse('plugins:netbox_windows_dhcp:settings'), data)
        self.assertHttpStatus(response, 302)
        self.assertTrue(DHCPPluginSettings.load().reservation_placeholders)

    def test_schedule_page_holds_run_now_and_schedule(self):
        response = self.client.get(reverse('plugins:netbox_windows_dhcp:schedule'))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, 'Run Now')
        self.assertContains(response, 'id="schedule-form"')
        settings_page = self.client.get(reverse('plugins:netbox_windows_dhcp:settings'))
        self.assertNotContains(settings_page, 'Run Now')

    def test_schedule_page_non_superuser_redirects(self):
        plain = get_user_model().objects.create_user('plain', password='x')
        c = Client()
        c.force_login(plain)
        response = c.get(reverse('plugins:netbox_windows_dhcp:schedule'))
        self.assertHttpStatus(response, 302)

    def test_schedule_run_now_enqueues_one_off_via_schedule_job(self):
        with mock.patch(SCHEDULE_JOB_ENQUEUE, return_value=job_mock()) as enq:
            self.client.post(reverse('plugins:netbox_windows_dhcp:schedule_sync'), {'action': 'run_now'})
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs.get('user'), self.user)
        self.assertFalse(enq.call_args.kwargs.get('recurring'))

    def test_schedule_future_reschedules_via_schedule_job(self):
        start_at = (timezone.now() + timedelta(days=1)).replace(microsecond=0)
        with mock.patch(SCHEDULE_JOB_ENQUEUE, return_value=job_mock()) as enq:
            self.client.post(reverse('plugins:netbox_windows_dhcp:schedule_sync'), {
                'action': 'schedule', 'start_at': start_at.isoformat(),
            })
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs.get('user'), self.user)
        self.assertTrue(enq.call_args.kwargs.get('recurring'))
        self.assertEqual(enq.call_args.kwargs.get('sync_at'), start_at)


class SettingsChangelogTests(TestCase):

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        self.url = reverse('plugins:netbox_windows_dhcp:settings')
        self.client.post(self.url, SETTINGS_FORM)  # a known starting point
        changes_for(DHCPPluginSettings).delete()

    def test_save_records_who_and_what_changed(self):
        before = DHCPPluginSettings.load().sync_interval
        response = self.client.post(self.url, {**SETTINGS_FORM, 'sync_interval': 120})
        self.assertHttpStatus(response, 302)

        change = changes_for(DHCPPluginSettings).get()
        self.assertEqual(change.user, self.user)
        self.assertEqual(change.action, 'update')
        self.assertEqual(change.prechange_data['sync_interval'], before)
        self.assertEqual(change.postchange_data['sync_interval'], 120)
        self.assertEqual(DHCPPluginSettings.load().get_absolute_url(), self.url)

    def test_save_with_no_changes_records_nothing(self):
        self.client.post(self.url, SETTINGS_FORM)
        self.assertFalse(changes_for(DHCPPluginSettings).exists())


class IPAddressLeaseHostnameColumnTests(TestCase):
    """The plugin registers an opt-in Lease Hostname column on the core IP Addresses table."""

    def test_column_renders_lease_hostname(self):
        from ipam.models import IPAddress
        from ipam.tables import IPAddressTable

        from ..models import DHCPLeaseInfo

        with_info = IPAddress.objects.create(address='10.9.0.1/24')
        IPAddress.objects.create(address='10.9.0.2/24')
        DHCPLeaseInfo.objects.create(ip_address=with_info, lease_hostname='My Dock Name', active=True)

        table = IPAddressTable(IPAddress.objects.filter(address__net_host_contained='10.9.0.0/24'))
        self.assertIn('dhcp_lease_hostname', table.columns.names())
        table.order_by = 'dhcp_lease_hostname'
        values = {str(row.record.address): row.get_cell_value('dhcp_lease_hostname') for row in table.rows}
        self.assertEqual(values['10.9.0.1/24'], 'My Dock Name')
        self.assertIsNone(values['10.9.0.2/24'])


class LeasePanelTimeZoneTests(TestCase):

    def test_label_shows_time_zone(self):
        self.user.is_superuser = True
        self.user.save()
        ip = IPAddress.objects.create(address='10.9.1.1/24', status='dhcp')
        DHCPLeaseInfo.objects.create(ip_address=ip, lease_hostname='h', active=True)
        from django.utils import timezone
        response = self.client.get(ip.get_absolute_url())
        self.assertContains(response, f'Lease Expiration ({timezone.get_current_timezone_name()})')


class LeasesViewTests(TestCase):
    """The Leases page: one row per IP the sync tracks, filterable on every column."""

    @classmethod
    def setUpTestData(cls):
        from extras.models import Tag

        cls.server = make_server(name='Srv A', hostname='a.example.com')
        cls.failover = make_failover(name='FO 1')
        cls.prefix = make_prefix('10.60.0.0/24')
        cls.scope = make_scope(name='Scope A', prefix=cls.prefix, server=cls.server)
        cls.fo_prefix = make_prefix('10.61.0.0/24')
        cls.fo_scope = make_scope(
            name='Scope FO', prefix=cls.fo_prefix, failover=cls.failover,
            start_ip='10.61.0.10', end_ip='10.61.0.20',
        )
        cls.tag = Tag.objects.create(name='Keep', slug='keep')
        cls.tenant = Tenant.objects.create(name='Tenant A', slug='tenant-a')
        cls.vrf = VRF.objects.create(name='Lease VRF')

        now = timezone.now()
        cls.res = IPAddress.objects.create(
            address='10.60.0.5/24', status='reserved', dns_name='res.example.com', description='Printer',
            custom_field_data={'dhcp_client_id': 'aa-bb-cc-dd-ee-01'},
        )
        cls.res.tags.add(cls.tag)
        DHCPLeaseInfo.objects.create(
            ip_address=cls.res, lease_hostname='PRINTER-1', active=False,
            state_changed=now - timedelta(days=30),
        )
        cls.lease = IPAddress.objects.create(address='10.60.0.6/24', status='dhcp', dns_name='pc.example.com')
        DHCPLeaseInfo.objects.create(
            ip_address=cls.lease, lease_hostname='LAPTOP-2', active=True,
            lease_expiration=now + timedelta(days=2), state_changed=now - timedelta(days=1),
        )
        cls.fo_ip = IPAddress.objects.create(address='10.61.0.7/24', status='reserved')
        DHCPLeaseInfo.objects.create(
            ip_address=cls.fo_ip, lease_hostname='fo-host', active=True, state_changed=now,
        )
        cls.stray = IPAddress.objects.create(address='10.99.0.1/24', status='dhcp', vrf=cls.vrf, tenant=cls.tenant)
        DHCPLeaseInfo.objects.create(ip_address=cls.stray, lease_hostname='stray', active=True)

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        self.url = ui_url('dhcpleaseinfo_list')

    def rows(self, **params):
        """The addresses the page lists for these filters."""
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return {str(row.record.ip_address.address.ip) for row in response.context['table'].rows}

    def test_every_tracked_ip_is_listed(self):
        self.assertEqual(self.rows(), {'10.60.0.5', '10.60.0.6', '10.61.0.7', '10.99.0.1'})

    def test_filters(self):
        now = timezone.now()
        cases = {
            'active': ({'active': 'true'}, {'10.60.0.6', '10.61.0.7', '10.99.0.1'}),
            'inactive': ({'active': 'false'}, {'10.60.0.5'}),
            'status': ({'status': 'reserved'}, {'10.60.0.5', '10.61.0.7'}),
            'scope': ({'scope_id': self.scope.pk}, {'10.60.0.5', '10.60.0.6'}),
            'parent prefix': ({'parent': '10.61.0.0/24'}, {'10.61.0.7'}),
            'parent prefix is a wider net': ({'parent': '10.60.0.0/15'}, {'10.60.0.5', '10.60.0.6', '10.61.0.7'}),
            'parent prefix not a prefix': ({'parent': 'nonsense'}, set()),
            'server': ({'server_id': self.server.pk}, {'10.60.0.5', '10.60.0.6'}),
            'server is not': ({'server_id__n': self.server.pk}, {'10.61.0.7', '10.99.0.1'}),
            'failover': ({'failover_id': self.failover.pk}, {'10.61.0.7'}),
            'failover is not': ({'failover_id__n': self.failover.pk}, {'10.60.0.5', '10.60.0.6', '10.99.0.1'}),
            'lease hostname is': ({'lease_hostname': 'LAPTOP-2'}, {'10.60.0.6'}),
            'lease hostname is not': ({'lease_hostname__n': 'LAPTOP-2'}, {'10.60.0.5', '10.61.0.7', '10.99.0.1'}),
            'lease hostname contains': ({'lease_hostname__ic': 'laptop'}, {'10.60.0.6'}),
            'lease hostname starts with': ({'lease_hostname__isw': 'fo-'}, {'10.61.0.7'}),
            'dns name is': ({'dns_name': 'res.example.com'}, {'10.60.0.5'}),
            'dns name is not': ({'dns_name__n': 'res.example.com'}, {'10.60.0.6', '10.61.0.7', '10.99.0.1'}),
            'dns name contains': ({'dns_name__ic': 'res.'}, {'10.60.0.5'}),
            'dns name ends with': ({'dns_name__iew': 'pc.example.com'}, {'10.60.0.6'}),
            'description': ({'description': 'print'}, {'10.60.0.5'}),
            'client id': ({'client_id': 'EE-01'}, {'10.60.0.5'}),
            'tag': ({'tag': 'keep'}, {'10.60.0.5'}),
            'vrf': ({'vrf_id': self.vrf.pk}, {'10.99.0.1'}),
            'vrf is not': ({'vrf_id__n': self.vrf.pk}, {'10.60.0.5', '10.60.0.6', '10.61.0.7'}),
            'vrf global': ({'vrf_id': 'null'}, {'10.60.0.5', '10.60.0.6', '10.61.0.7'}),
            'tenant': ({'tenant_id': self.tenant.pk}, {'10.99.0.1'}),
            'tenant none': ({'tenant_id': 'null'}, {'10.60.0.5', '10.60.0.6', '10.61.0.7'}),
            'search hostname': ({'q': 'stray'}, {'10.99.0.1'}),
            'search scope name': ({'q': 'Scope FO'}, {'10.61.0.7'}),
            'expiration after': ({'expiration_after': (now + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')},
                                 {'10.60.0.6'}),
            'expiration before': ({'expiration_before': (now + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')},
                                  set()),
            'state since after': ({'state_changed_after': (now - timedelta(days=10)).strftime('%Y-%m-%d %H:%M:%S')},
                                  {'10.60.0.6', '10.61.0.7'}),
            'state since before': ({'state_changed_before': (now - timedelta(days=10)).strftime('%Y-%m-%d %H:%M:%S')},
                                   {'10.60.0.5'}),
        }
        for name, (params, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.rows(**params), expected)

    def test_filter_form_lists_statuses_and_offers_modifiers(self):
        response = self.client.get(self.url)
        content = response.content.decode()
        for status in ('reserved', 'dhcp'):
            with self.subTest(status=status):
                self.assertIn(f'value="{status}"', content)
        for field in ('lease_hostname', 'dns_name', 'server_id', 'failover_id', 'vrf_id', 'tenant_id'):
            with self.subTest(modifiers=field):
                self.assertIn(f'data-field="{field}"', content)   # the is / is not / contains dropdown
        self.assertNotIn('name="address"', content)

    def test_window_filter_combines_after_and_before(self):
        now = timezone.now()
        window = {
            'state_changed_after': (now - timedelta(days=40)).strftime('%Y-%m-%d %H:%M:%S'),
            'state_changed_before': (now - timedelta(days=10)).strftime('%Y-%m-%d %H:%M:%S'),
        }
        self.assertEqual(self.rows(**window), {'10.60.0.5'})

    def test_scope_is_the_one_whose_prefix_and_vrf_hold_the_ip(self):
        vrf = VRF.objects.create(name='Other')
        in_vrf = IPAddress.objects.create(address='10.60.0.8/24', vrf=vrf, status='dhcp')
        DHCPLeaseInfo.objects.create(ip_address=in_vrf, active=True)
        narrow = make_scope(
            name='Narrow', prefix=make_prefix('10.60.0.4/31'), server=make_server(name='Srv N', hostname='n.example.com'),
            start_ip='10.60.0.4', end_ip='10.60.0.5',
        )
        response = self.client.get(self.url)
        names = dict(DHCPScope.objects.values_list('pk', 'name'))
        scopes = {
            str(row.record.ip_address.address.ip): names.get(row.record.scope_pk)
            for row in response.context['table'].rows
        }
        self.assertEqual(scopes['10.60.0.5'], 'Narrow')   # nested scopes: the narrowest wins
        self.assertEqual(scopes['10.60.0.6'], 'Scope A')
        self.assertIsNone(scopes['10.60.0.8'])            # another VRF: not this scope's IP
        self.assertIsNone(scopes['10.99.0.1'])            # no scope at all
        self.assertEqual(narrow.name, 'Narrow')

    def test_list_sorts_by_scope_name(self):
        for direction in ('', '-'):
            with self.subTest(direction=direction):
                response = self.client.get(self.url, {'sort': f'{direction}scope'})
                self.assertEqual(response.status_code, 200)
                names = [
                    DHCPScope.objects.get(pk=row.record.scope_pk).name
                    for row in response.context['table'].rows if row.record.scope_pk
                ]
                self.assertEqual(names, sorted(names, reverse=bool(direction)))
                self.assertGreater(len(set(names)), 1)

    def test_every_column_renders_and_the_table_exports(self):
        from ..tables import DHCPLeaseTable
        from ..utils import with_scope

        table = DHCPLeaseTable(with_scope(DHCPLeaseInfo.objects.all()))
        for name in DHCPLeaseTable.Meta.fields:
            table.columns.show(name)
        for row in table.rows:
            for name in DHCPLeaseTable.Meta.fields:
                with self.subTest(column=name):
                    self.assertIsNotNone(row.get_cell(name))

        response = self.client.get(self.url, {'export': 'table'})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        for ip in ('10.60.0.5', '10.60.0.6', '10.61.0.7', '10.99.0.1'):
            self.assertIn(ip, body)

    def test_sees_only_ips_the_user_may_view(self):
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(self.rows(), set())
        grant(self.user, IPAddress, ['view'], address__net_host='10.60.0.6')
        self.assertEqual(self.rows(), {'10.60.0.6'})


class ScopeTableColumnTests(TestCase):
    """Every column of the Scopes list renders, including the ones that come from the prefix."""

    def test_every_column_renders(self):
        from dcim.models import Site
        from tenancy.models import Tenant

        from ..tables import DHCPScopeTable

        site = Site.objects.create(name='Col Site', slug='col-site')
        tenant = Tenant.objects.create(name='Col Tenant', slug='col-tenant')
        make_scope(
            name='With Prefix', prefix=make_prefix('10.70.0.0/24', tenant=tenant, scope=site),
            server=make_server(name='Col Srv', hostname='col.example.com'),
        )
        make_unassigned_scope(
            name='No Prefix', network='10.71.0.0', server=DHCPServer.objects.get(name='Col Srv'),
            start_ip='10.71.0.10', end_ip='10.71.0.20',
        )
        table = DHCPScopeTable(DHCPScope.objects.select_related('prefix'))
        for name in DHCPScopeTable.Meta.fields:
            table.columns.show(name)
        cells = {}
        for row in table.rows:
            for name in DHCPScopeTable.Meta.fields:
                with self.subTest(scope=row.record.name, column=name):
                    cells[(row.record.name, name)] = str(row.get_cell(name))
        self.assertIn('Col Site', cells[('With Prefix', 'site')])
        self.assertIn('Col Tenant', cells[('With Prefix', 'tenant')])
        self.assertIn('Global', cells[('With Prefix', 'vrf')])
        self.assertNotIn('Global', cells[('No Prefix', 'vrf')])
