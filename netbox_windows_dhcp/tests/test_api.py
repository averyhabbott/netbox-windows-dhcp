"""
REST API: NetBox's CRUD harness for each model, plus API-only behavior.

The shared CRUD mixin sits inside a container class (``_APISuite``) so the loader
doesn't collect it as a test. The API mixins are composed one by one, leaving out
GraphQL, which the plugin doesn't offer.
"""

import json
from unittest import mock

from django.urls import reverse
from ipam.models import IPAddress, Prefix, VRF
from utilities.testing import APITestCase, APIViewTestCases

from ..models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPLeaseInfo,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)
from .base import (
    changes_for,
    grant,
    PluginAPIViewTestMixin,
    api_url,
    clear_builtin_option_codes,
    make_failover,
    make_option_definition,
    make_option_value,
    make_scope,
    make_server,
    set_plugin_settings,
)


TOKEN = 'app-token-that-must-not-leak-0123456789'
SCOPE_PUSH = 'netbox_windows_dhcp.signals._queue_scope_push'


class _APISuite:
    """Container so the shared mixin isn't discovered as a test case on its own."""

    class CRUD(
        PluginAPIViewTestMixin,
        APIViewTestCases.GetObjectViewTestCase,
        APIViewTestCases.ListObjectsViewTestCase,
        APIViewTestCases.CreateObjectViewTestCase,
        APIViewTestCases.UpdateObjectViewTestCase,
        APIViewTestCases.DeleteObjectViewTestCase,
    ):
        pass


class _OptionCodeFixtures:

    def _fixtures(self):
        set_plugin_settings(push_scope_info=True)
        code = make_option_definition(code=210, name='Opt 210')
        other = make_option_definition(code=211, name='Opt 211')
        self.a = make_option_value(option_definition=code, value='1.1.1.1', friendly_name='A')
        self.b = make_option_value(option_definition=code, value='2.2.2.2', friendly_name='B')
        self.c = make_option_value(option_definition=other, value='3.3.3.3', friendly_name='C')
        self.scope = make_scope()


class DHCPServerAPITests(_APISuite.CRUD):
    model = DHCPServer
    brief_fields = ['display', 'hostname', 'id', 'name', 'url']
    update_data = {'port': 8080}
    bulk_update_data = {'sync_standalone_scopes': False}
    bulk_update_invalid_data = {'port': -1}

    @classmethod
    def setUpTestData(cls):
        DHCPServer.objects.bulk_create([
            DHCPServer(name='Server 1', hostname='s1.example.com'),
            DHCPServer(name='Server 2', hostname='s2.example.com'),
            DHCPServer(name='Server 3', hostname='s3.example.com'),
        ])
        cls.create_data = [
            {'name': 'Server 4', 'hostname': 's4.example.com'},
            {'name': 'Server 5', 'hostname': 's5.example.com', 'port': 8443},
            {'name': 'Server 6', 'hostname': 's6.example.com', 'use_https': False},
        ]


class DHCPFailoverAPITests(
    PluginAPIViewTestMixin,
    APIViewTestCases.GetObjectViewTestCase,
    APIViewTestCases.ListObjectsViewTestCase,
    APIViewTestCases.UpdateObjectViewTestCase,
    APIViewTestCases.DeleteObjectViewTestCase,
):
    # No create: failovers are import-only (see FailoverAPILockTests).
    model = DHCPFailover
    brief_fields = ['display', 'id', 'mode', 'name', 'url']
    update_data = {'description': 'Updated failover'}
    bulk_update_data = {'description': 'Bulk described'}
    bulk_update_invalid_data = {'enable_auth': True, 'shared_secret': ''}

    @classmethod
    def setUpTestData(cls):
        servers = DHCPServer.objects.bulk_create([
            DHCPServer(name=f'FoSrv {i}', hostname=f'fo{i}.example.com') for i in range(1, 9)
        ])
        DHCPFailover.objects.bulk_create([
            DHCPFailover(name='FO 1', primary_server=servers[0], secondary_server=servers[1]),
            DHCPFailover(name='FO 2', primary_server=servers[2], secondary_server=servers[3]),
            DHCPFailover(name='FO 3', primary_server=servers[4], secondary_server=servers[5]),
        ])


class DHCPOptionCodeDefinitionAPITests(_APISuite.CRUD):
    model = DHCPOptionCodeDefinition
    brief_fields = ['code', 'data_type', 'display', 'id', 'name', 'url']
    update_data = {'description': 'updated'}
    bulk_update_data = {'description': 'bulk update'}
    bulk_update_invalid_data = {'name': ''}

    @classmethod
    def setUpTestData(cls):
        # Clear the migration-seeded built-ins so list/bulk-delete counts are
        # deterministic and not blocked by the is_builtin delete guard.
        clear_builtin_option_codes()
        DHCPOptionCodeDefinition.objects.bulk_create([
            DHCPOptionCodeDefinition(code=200, name='Opt 200'),
            DHCPOptionCodeDefinition(code=201, name='Opt 201'),
            DHCPOptionCodeDefinition(code=202, name='Opt 202'),
        ])
        cls.create_data = [
            {'code': 203, 'name': 'Opt 203', 'data_type': 'String'},
            {'code': 204, 'name': 'Opt 204', 'data_type': 'IPAddress'},
            {'code': 205, 'name': 'Opt 205', 'data_type': 'String'},
        ]


class DHCPOptionValueAPITests(_APISuite.CRUD):
    model = DHCPOptionValue
    brief_fields = ['display', 'friendly_name', 'id', 'url', 'value']
    update_data = {'friendly_name': 'updated'}
    bulk_update_data = {'friendly_name': 'bulk update'}
    bulk_update_invalid_data = {'value': ''}
    validation_excluded_fields = ['option_definition_id']

    @classmethod
    def setUpTestData(cls):
        opt = DHCPOptionCodeDefinition.objects.create(code=200, name='DNS')
        DHCPOptionValue.objects.bulk_create([
            DHCPOptionValue(option_definition=opt, value='10.0.0.1', friendly_name='V1'),
            DHCPOptionValue(option_definition=opt, value='10.0.0.2', friendly_name='V2'),
            DHCPOptionValue(option_definition=opt, value='10.0.0.3', friendly_name='V3'),
        ])
        cls.create_data = [
            {'option_definition_id': opt.pk, 'value': '10.0.0.4', 'friendly_name': 'V4'},
            {'option_definition_id': opt.pk, 'value': '10.0.0.5', 'friendly_name': 'V5'},
            {'option_definition_id': opt.pk, 'value': '10.0.0.6', 'friendly_name': 'V6'},
        ]
        # Option value writes are refused while push_scope_info is off.
        set_plugin_settings(push_scope_info=True)


class DHCPScopeAPITests(_APISuite.CRUD):
    model = DHCPScope
    brief_fields = ['display', 'end_ip', 'id', 'name', 'start_ip', 'url']
    update_data = {'lease_lifetime': 7200, 'description': 'Updated scope'}
    bulk_update_data = {'lease_lifetime': 43200}
    bulk_update_invalid_data = {'end_ip': '10.0.0.1'}  # before every scope's start
    validation_excluded_fields = ['prefix_id', 'server_id']

    @classmethod
    def setUpTestData(cls):
        # One scope per prefix, and one per network on a server.
        prefixes = [Prefix.objects.create(prefix=f'10.0.{i}.0/24', status='active') for i in range(1, 6)]
        server = DHCPServer.objects.create(name='ScopeSrv', hostname='scopesrv.example.com')
        cls.server = server
        DHCPScope.objects.bulk_create([
            DHCPScope(name=f'Scope {i}', prefix=prefixes[i - 1], network=f'10.0.{i}.0', prefix_length=24,
                      server=server, start_ip=f'10.0.{i}.10', end_ip=f'10.0.{i}.20')
            for i in range(1, 4)
        ])
        cls.create_data = [
            {'name': 'Scope 4', 'prefix_id': prefixes[3].pk, 'server_id': server.pk,
             'start_ip': '10.0.4.70', 'end_ip': '10.0.4.80', 'description': 'Fourth floor'},
            {'name': 'Scope 5', 'prefix_id': prefixes[4].pk, 'server_id': server.pk,
             'start_ip': '10.0.5.90', 'end_ip': '10.0.5.100'},
            # No prefix: network and length instead.
            {'name': 'Scope 6', 'network': '10.0.6.0', 'prefix_length': 24, 'server_id': server.pk,
             'start_ip': '10.0.6.110', 'end_ip': '10.0.6.120'},
        ]
        # Scope writes are refused while push_scope_info is off.
        set_plugin_settings(push_scope_info=True)


class DHCPExclusionRangeAPITests(_APISuite.CRUD):
    model = DHCPExclusionRange
    brief_fields = ['display', 'end_ip', 'id', 'start_ip', 'url']
    update_data = {'end_ip': '10.0.1.249', 'description': 'Printers'}
    bulk_update_data = {'end_ip': '10.0.1.50'}
    bulk_update_invalid_data = {'end_ip': '10.0.1.1'}  # before every exclusion's start
    validation_excluded_fields = ['scope_id']

    @classmethod
    def setUpTestData(cls):
        prefix = Prefix.objects.create(prefix='10.0.1.0/24', status='active')
        server = DHCPServer.objects.create(name='ExSrv', hostname='exsrv.example.com')
        scope = DHCPScope.objects.create(
            name='ExScope', prefix=prefix, server=server, start_ip='10.0.1.10', end_ip='10.0.1.254',
        )
        cls.scope = scope
        DHCPExclusionRange.objects.bulk_create([
            DHCPExclusionRange(scope=scope, start_ip='10.0.1.20', end_ip='10.0.1.25'),
            DHCPExclusionRange(scope=scope, start_ip='10.0.1.30', end_ip='10.0.1.35'),
            DHCPExclusionRange(scope=scope, start_ip='10.0.1.40', end_ip='10.0.1.45'),
        ])
        cls.create_data = [
            {'scope_id': scope.pk, 'start_ip': '10.0.1.60', 'end_ip': '10.0.1.65', 'description': 'Static'},
            {'scope_id': scope.pk, 'start_ip': '10.0.1.70', 'end_ip': '10.0.1.75'},
            {'scope_id': scope.pk, 'start_ip': '10.0.1.80', 'end_ip': '10.0.1.85'},
        ]
        # Exclusion writes are refused while push_scope_info is off.
        set_plugin_settings(push_scope_info=True)


class APIGateAndSecurityTests(APITestCase):
    """The api_enabled 503 gate and the write-only api_key field."""

    model = DHCPServer

    @classmethod
    def setUpTestData(cls):
        cls.server = DHCPServer.objects.create(
            name='Gate Server', hostname='gate.example.com', api_key='super-secret',
        )

    def test_api_disabled_returns_503(self):
        set_plugin_settings(api_enabled=False)
        self.add_permissions('netbox_windows_dhcp.view_dhcpserver', 'ipam.view_ipaddress')
        for endpoint in ('dhcpserver', 'dhcpleaseinfo'):
            with self.subTest(endpoint=endpoint):
                self.assertHttpStatus(self.client.get(api_url(endpoint), **self.header), 503)

    def test_api_key_is_write_only(self):
        set_plugin_settings(api_enabled=True)
        self.add_permissions('netbox_windows_dhcp.view_dhcpserver')
        url = reverse('plugins-api:netbox_windows_dhcp-api:dhcpserver-detail', kwargs={'pk': self.server.pk})
        response = self.client.get(url, **self.header)
        self.assertHttpStatus(response, 200)
        self.assertNotIn('api_key', response.json())

    def test_status_fields_are_exposed_read_only(self):
        set_plugin_settings(api_enabled=True)
        self.add_permissions('netbox_windows_dhcp.view_dhcpserver', 'netbox_windows_dhcp.change_dhcpserver')
        DHCPServer.objects.filter(pk=self.server.pk).update(
            health_status='healthy', access_level='ro', psu_script_version='1.1.2',
        )
        url = reverse('plugins-api:netbox_windows_dhcp-api:dhcpserver-detail', kwargs={'pk': self.server.pk})

        data = self.client.get(url, **self.header).json()
        self.assertEqual(data['health_status']['value'], 'healthy')
        self.assertEqual(data['access_level']['value'], 'ro')
        self.assertEqual(data['psu_script_version'], '1.1.2')
        for field in ('last_health_check', 'health_error', 'last_sync_at', 'last_sync_error'):
            self.assertIn(field, data)

        # Writes to status fields are silently ignored (read-only), not applied.
        response = self.client.patch(
            url,
            {'health_status': 'unreachable', 'access_level': 'rw', 'psu_script_version': '9.9.9'},
            format='json', **self.header,
        )
        self.assertHttpStatus(response, 200)
        self.server.refresh_from_db()
        self.assertEqual(self.server.health_status, 'healthy')
        self.assertEqual(self.server.access_level, 'ro')
        self.assertEqual(self.server.psu_script_version, '1.1.2')


class TokenNotInChangelogTests(APITestCase):

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()

    def test_snapshot_masks_the_token(self):
        self.assertEqual(make_server(api_key=TOKEN).serialize_object()['api_key'], '********')
        self.assertEqual(
            make_server(name='No token', hostname='none.example.com').serialize_object()['api_key'], ''
        )

    def test_api_create_and_update_never_record_the_token(self):
        url = reverse('plugins-api:netbox_windows_dhcp-api:dhcpserver-list')
        response = self.client.post(
            url, {'name': 'Leak check', 'hostname': 'leak.example.com', 'api_key': TOKEN},
            format='json', **self.header,
        )
        self.assertHttpStatus(response, 201)
        server = DHCPServer.objects.get(name='Leak check')
        self.assertEqual(server.api_key, TOKEN)  # still stored and usable

        detail = reverse('plugins-api:netbox_windows_dhcp-api:dhcpserver-detail', kwargs={'pk': server.pk})
        response = self.client.patch(
            detail, {'api_key': TOKEN + '-new', 'port': 8443}, format='json', **self.header,
        )
        self.assertHttpStatus(response, 200)

        changes = changes_for(DHCPServer).filter(changed_object_id=server.pk)
        self.assertEqual(changes.count(), 2)
        for change in changes:
            self.assertNotIn(TOKEN, json.dumps([change.prechange_data, change.postchange_data]))
            self.assertEqual(change.postchange_data['api_key'], '********')


class ServerVRFAPITests(APITestCase):

    def test_default_scope_vrf_is_writable(self):
        self.add_permissions('netbox_windows_dhcp.view_dhcpserver', 'netbox_windows_dhcp.change_dhcpserver',
                             'ipam.view_vrf')
        server = make_server()
        red = VRF.objects.create(name='Red')
        url = reverse('plugins-api:netbox_windows_dhcp-api:dhcpserver-detail', kwargs={'pk': server.pk})
        response = self.client.patch(url, {'default_scope_vrf_id': red.pk}, format='json', **self.header)
        self.assertHttpStatus(response, 200)
        server.refresh_from_db()
        self.assertEqual(server.default_scope_vrf, red)


class BuiltinOptionCodeAPITests(APITestCase):

    def setUp(self):
        super().setUp()
        self.add_permissions(
            'netbox_windows_dhcp.view_dhcpoptioncodedefinition',
            'netbox_windows_dhcp.add_dhcpoptioncodedefinition',
            'netbox_windows_dhcp.change_dhcpoptioncodedefinition',
        )

    def test_is_builtin_is_shown_but_never_set(self):
        builtin = DHCPOptionCodeDefinition.objects.filter(is_builtin=True).first()
        response = self.client.patch(api_url('dhcpoptioncodedefinition', builtin.pk), {'is_builtin': False},
                                     format='json', **self.header)
        self.assertHttpStatus(response, 200)
        self.assertTrue(response.json()['is_builtin'])
        builtin.refresh_from_db()
        self.assertTrue(builtin.is_builtin)

        response = self.client.post(api_url('dhcpoptioncodedefinition'),
                                    {'code': 230, 'name': 'New', 'data_type': 'String', 'is_builtin': True},
                                    format='json', **self.header)
        self.assertHttpStatus(response, 201)
        self.assertFalse(DHCPOptionCodeDefinition.objects.get(code=230).is_builtin)


class DuplicateOptionCodeAPITests(_OptionCodeFixtures, APITestCase):

    def setUp(self):
        super().setUp()
        self.add_permissions('netbox_windows_dhcp.view_dhcpscope', 'netbox_windows_dhcp.change_dhcpscope',
                             'netbox_windows_dhcp.view_dhcpoptionvalue')
        self._fixtures()

    def test_api_refuses_two_values_for_one_code(self):
        with mock.patch(SCOPE_PUSH):
            response = self.client.patch(api_url('dhcpscope', self.scope.pk),
                                         {'option_value_ids': [self.a.pk, self.b.pk]},
                                         format='json', **self.header)
        self.assertHttpStatus(response, 400)
        self.assertIn('210', str(response.json()))
        self.assertEqual(self.scope.option_values.count(), 0)

    def test_api_accepts_distinct_codes(self):
        with mock.patch(SCOPE_PUSH):
            response = self.client.patch(api_url('dhcpscope', self.scope.pk),
                                         {'option_value_ids': [self.a.pk, self.c.pk]},
                                         format='json', **self.header)
        self.assertHttpStatus(response, 200)
        self.assertEqual(self.scope.option_values.count(), 2)


class MaintenanceAPITests(APITestCase):

    def setUp(self):
        super().setUp()
        for model in ('dhcpserver', 'dhcpfailover', 'dhcpscope'):
            self.add_permissions(f'netbox_windows_dhcp.view_{model}', f'netbox_windows_dhcp.change_{model}')
        self.server = make_server()

    def test_shown_on_all_three(self):
        failover = make_failover()
        scope = make_scope(server=self.server)
        for name, obj in (('dhcpserver', self.server), ('dhcpfailover', failover), ('dhcpscope', scope)):
            with self.subTest(name=name):
                data = self.client.get(api_url(name, obj.pk), **self.header).json()
                for field in ('maintenance_mode', 'maintenance_notes', 'maintenance_enabled_at',
                              'maintenance_enabled_by'):
                    self.assertIn(field, data)

    def test_turning_on_records_who_and_when_and_off_clears_them(self):
        url = api_url('dhcpserver', self.server.pk)
        response = self.client.patch(url, {'maintenance_mode': True, 'maintenance_notes': 'patching'},
                                     format='json', **self.header)
        self.assertHttpStatus(response, 200)
        self.server.refresh_from_db()
        self.assertTrue(self.server.maintenance_mode)
        self.assertEqual(self.server.maintenance_notes, 'patching')
        self.assertEqual(self.server.maintenance_enabled_by, self.user)
        self.assertIsNotNone(self.server.maintenance_enabled_at)
        self.assertEqual(response.json()['maintenance_enabled_by']['id'], self.user.pk)

        self.client.patch(url, {'maintenance_mode': False}, format='json', **self.header)
        self.server.refresh_from_db()
        self.assertEqual(
            (self.server.maintenance_mode, self.server.maintenance_notes,
             self.server.maintenance_enabled_by, self.server.maintenance_enabled_at),
            (False, '', None, None),
        )

    def test_who_and_when_cannot_be_set(self):
        self.client.patch(api_url('dhcpserver', self.server.pk),
                          {'maintenance_enabled_at': '2020-01-01T00:00:00Z'}, format='json', **self.header)
        self.server.refresh_from_db()
        self.assertIsNone(self.server.maintenance_enabled_at)

    def test_notes_change_while_on_keeps_who_and_when(self):
        url = api_url('dhcpserver', self.server.pk)
        self.client.patch(url, {'maintenance_mode': True}, format='json', **self.header)
        self.server.refresh_from_db()
        when = self.server.maintenance_enabled_at
        self.client.patch(url, {'maintenance_notes': 'later'}, format='json', **self.header)
        self.server.refresh_from_db()
        self.assertEqual((self.server.maintenance_notes, self.server.maintenance_enabled_at), ('later', when))

    def test_scope_maintenance_allowed_with_push_scope_info_off(self):
        set_plugin_settings(push_scope_info=False)
        scope = make_scope(server=self.server)
        response = self.client.patch(api_url('dhcpscope', scope.pk), {'maintenance_mode': True},
                                     format='json', **self.header)
        self.assertHttpStatus(response, 200)
        scope.refresh_from_db()
        self.assertTrue(scope.maintenance_mode)

    def test_failover_maintenance_and_sync_enabled_can_change(self):
        failover = make_failover()
        response = self.client.patch(api_url('dhcpfailover', failover.pk),
                                     {'maintenance_mode': True, 'sync_enabled': False},
                                     format='json', **self.header)
        self.assertHttpStatus(response, 200)
        failover.refresh_from_db()
        self.assertEqual((failover.maintenance_mode, failover.sync_enabled), (True, False))
        self.assertEqual(failover.maintenance_enabled_by, self.user)

    def test_maintenance_and_sync_filters(self):
        make_server(name='Other', hostname='other.example.com', maintenance_mode=True)
        response = self.client.get(api_url('dhcpserver') + '?maintenance_mode=true', **self.header)
        self.assertEqual([s['name'] for s in response.json()['results']], ['Other'])

        make_failover(name='Off', primary=make_server(name='P2', hostname='p2'),
                      secondary=make_server(name='S2', hostname='s2'), sync_enabled=False)
        make_failover()
        response = self.client.get(api_url('dhcpfailover') + '?sync_enabled=false', **self.header)
        self.assertEqual([f['name'] for f in response.json()['results']], ['Off'])


class CertificateStatusAPITests(APITestCase):

    def test_status_shown_certificate_hidden(self):
        self.add_permissions('netbox_windows_dhcp.view_dhcpserver')
        server = make_server(ca_cert='-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----')
        data = self.client.get(api_url('dhcpserver', server.pk), **self.header).json()
        self.assertTrue(data['has_ca_cert'])
        self.assertIn('ca_cert_expiry', data)
        self.assertNotIn('ca_cert', data)


class LeaseInfoAPITests(APITestCase):

    def setUp(self):
        super().setUp()
        self.ip_a = IPAddress.objects.create(address='10.9.0.1/24', status='dhcp')
        self.ip_b = IPAddress.objects.create(address='10.9.0.2/24', status='reserved')
        DHCPLeaseInfo.objects.create(ip_address=self.ip_a, lease_hostname='alpha', active=True)
        self.info_b = DHCPLeaseInfo.objects.create(ip_address=self.ip_b, lease_hostname='beta', active=False)

    def test_lists_only_entries_for_viewable_ips(self):
        grant(self.user, IPAddress, ['view'], pk=self.ip_a.pk)
        data = self.client.get(api_url('dhcpleaseinfo'), **self.header).json()
        self.assertEqual([r['lease_hostname'] for r in data['results']], ['alpha'])
        response = self.client.get(api_url('dhcpleaseinfo', self.info_b.pk), **self.header)
        self.assertHttpStatus(response, 404)

    def test_without_ip_permission_nothing_is_listed(self):
        data = self.client.get(api_url('dhcpleaseinfo'), **self.header).json()
        self.assertEqual(data['count'], 0)

    def test_filters(self):
        self.add_permissions('ipam.view_ipaddress')
        for query, expected in (('active=false', ['beta']), ('lease_hostname=alp', ['alpha']),
                                ('address=10.9.0.2', ['beta']), (f'ip_address_id={self.ip_a.pk}', ['alpha'])):
            with self.subTest(query=query):
                data = self.client.get(api_url('dhcpleaseinfo') + '?' + query, **self.header).json()
                self.assertEqual([r['lease_hostname'] for r in data['results']], expected)

    def test_read_only(self):
        self.add_permissions('ipam.view_ipaddress', 'ipam.change_ipaddress')
        response = self.client.patch(api_url('dhcpleaseinfo', self.info_b.pk), {'active': True},
                                     format='json', **self.header)
        self.assertIn(response.status_code, (403, 405))
        self.info_b.refresh_from_db()
        self.assertFalse(self.info_b.active)
