"""
REST API tests using NetBox's APIViewTestCases harness (CRUD + permissions),
plus explicit coverage of the api_enabled 503 gate and the write-only api_key.

The shared CRUD mixin is nested inside a container class (``_APISuite``) so the
test loader does not collect it as a standalone test case — only the concrete
per-model subclasses run. We compose the individual API mixins rather than the
combined ``APIViewTestCases.APIViewTestCase`` to exclude ``GraphQLTestCase``,
since the plugin does not implement a GraphQL schema.
"""

from django.urls import reverse
from ipam.models import Prefix
from utilities.testing import APITestCase, APIViewTestCases

from ..models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)
from .base import PluginAPIViewTestMixin, clear_builtin_option_codes, set_plugin_settings


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


class DHCPServerAPITests(_APISuite.CRUD):
    model = DHCPServer
    brief_fields = ['display', 'hostname', 'id', 'name', 'url']
    update_data = {'port': 8080}
    bulk_update_data = {'sync_standalone_scopes': False}

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


class DHCPFailoverAPITests(_APISuite.CRUD):
    model = DHCPFailover
    brief_fields = ['display', 'id', 'mode', 'name', 'url']
    update_data = {'max_response_delay': 77}
    bulk_update_data = {'max_client_lead_time': 7200}
    # Write-only FK inputs are not echoed in the response (read side is nested).
    validation_excluded_fields = ['primary_server_id', 'secondary_server_id']

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
        cls.create_data = [
            {'name': 'FO 4', 'primary_server_id': servers[6].pk, 'secondary_server_id': servers[7].pk},
        ]


class DHCPOptionCodeDefinitionAPITests(_APISuite.CRUD):
    model = DHCPOptionCodeDefinition
    brief_fields = ['code', 'data_type', 'display', 'id', 'name', 'url']
    update_data = {'description': 'updated'}
    bulk_update_data = {'description': 'bulk update'}

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


class DHCPScopeAPITests(_APISuite.CRUD):
    model = DHCPScope
    brief_fields = ['display', 'end_ip', 'id', 'name', 'start_ip', 'url']
    update_data = {'lease_lifetime': 7200}
    bulk_update_data = {'lease_lifetime': 43200}
    validation_excluded_fields = ['prefix_id', 'server_id']

    @classmethod
    def setUpTestData(cls):
        prefix = Prefix.objects.create(prefix='10.0.1.0/24', status='active')
        server = DHCPServer.objects.create(name='ScopeSrv', hostname='scopesrv.example.com')
        cls.server = server
        cls.prefix = prefix
        DHCPScope.objects.bulk_create([
            DHCPScope(name='Scope 1', prefix=prefix, server=server, start_ip='10.0.1.10', end_ip='10.0.1.20'),
            DHCPScope(name='Scope 2', prefix=prefix, server=server, start_ip='10.0.1.30', end_ip='10.0.1.40'),
            DHCPScope(name='Scope 3', prefix=prefix, server=server, start_ip='10.0.1.50', end_ip='10.0.1.60'),
        ])
        cls.create_data = [
            {'name': 'Scope 4', 'prefix_id': prefix.pk, 'server_id': server.pk,
             'start_ip': '10.0.1.70', 'end_ip': '10.0.1.80'},
            {'name': 'Scope 5', 'prefix_id': prefix.pk, 'server_id': server.pk,
             'start_ip': '10.0.1.90', 'end_ip': '10.0.1.100'},
            {'name': 'Scope 6', 'prefix_id': prefix.pk, 'server_id': server.pk,
             'start_ip': '10.0.1.110', 'end_ip': '10.0.1.120'},
        ]


class DHCPExclusionRangeAPITests(_APISuite.CRUD):
    model = DHCPExclusionRange
    brief_fields = ['display', 'end_ip', 'id', 'start_ip', 'url']
    update_data = {'end_ip': '10.0.1.249'}
    validation_excluded_fields = ['scope_id']
    # No safe scalar to bulk-update (scope/start/end form the identity); skip bulk update.

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
            {'scope_id': scope.pk, 'start_ip': '10.0.1.60', 'end_ip': '10.0.1.65'},
            {'scope_id': scope.pk, 'start_ip': '10.0.1.70', 'end_ip': '10.0.1.75'},
            {'scope_id': scope.pk, 'start_ip': '10.0.1.80', 'end_ip': '10.0.1.85'},
        ]


class APIGateAndSecurityTests(APITestCase):
    """The api_enabled 503 gate and the write-only api_key field."""

    model = DHCPServer

    @classmethod
    def setUpTestData(cls):
        cls.server = DHCPServer.objects.create(
            name='Gate Server', hostname='gate.example.com', api_key='super-secret',
        )

    def _list_url(self):
        return reverse('plugins-api:netbox_windows_dhcp-api:dhcpserver-list')

    def test_api_disabled_returns_503(self):
        set_plugin_settings(api_enabled=False)
        self.add_permissions('netbox_windows_dhcp.view_dhcpserver')
        response = self.client.get(self._list_url(), **self.header)
        self.assertHttpStatus(response, 503)

    def test_api_enabled_returns_200(self):
        set_plugin_settings(api_enabled=True)
        self.add_permissions('netbox_windows_dhcp.view_dhcpserver')
        response = self.client.get(self._list_url(), **self.header)
        self.assertHttpStatus(response, 200)

    def test_api_key_is_write_only(self):
        set_plugin_settings(api_enabled=True)
        self.add_permissions('netbox_windows_dhcp.view_dhcpserver')
        url = reverse('plugins-api:netbox_windows_dhcp-api:dhcpserver-detail', kwargs={'pk': self.server.pk})
        response = self.client.get(url, **self.header)
        self.assertHttpStatus(response, 200)
        self.assertNotIn('api_key', response.json())
