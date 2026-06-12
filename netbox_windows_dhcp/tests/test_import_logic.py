"""
Import-pipeline tests. A FakePSUClient supplies canned PSU payloads, so no
network is used. Exercises both snake_case and PascalCase PSU response shapes.
"""

from unittest import mock

from django.test import TestCase
from ipam.models import Prefix

from ..import_logic import _import_failover, _import_scope, run_import
from ..models import DHCPExclusionRange, DHCPFailover, DHCPScope
from .base import (
    FAKE_EXCLUSION,
    FAKE_SCOPE_PASCAL,
    FAKE_SCOPE_SNAKE,
    FakePSUClient,
    make_scope,
    make_server,
    set_plugin_settings,
)


def fresh_results():
    return {
        'failovers':        {'created': [], 'skipped': [], 'errors': []},
        'scopes':           {'created': [], 'skipped': [], 'errors': []},
        'option_values':    {'created': [], 'skipped': [], 'errors': []},
        'exclusion_ranges': {'created': [], 'skipped': [], 'errors': []},
    }


class ImportFailoverTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.primary = make_server(name='P', hostname='p.example.com')
        cls.secondary = make_server(name='S', hostname='s.example.com')

    def test_creates_failover_from_snake_payload(self):
        results = fresh_results()
        _import_failover({
            'name': 'FO1', 'primary_server': 'p.example.com',
            'secondary_server': 's.example.com', 'mode': 'HotStandby',
        }, results)
        fo = DHCPFailover.objects.get(name='FO1')
        self.assertEqual(fo.primary_server, self.primary)
        self.assertEqual(fo.secondary_server, self.secondary)
        self.assertEqual(fo.mode, 'HotStandby')
        self.assertIn('FO1', results['failovers']['created'])

    def test_pascal_keys_resolve(self):
        results = fresh_results()
        _import_failover({
            'Name': 'FO2', 'PrimaryServer': 'p.example.com',
            'SecondaryServer': 's.example.com', 'Mode': 'LoadBalance',
        }, results)
        self.assertTrue(DHCPFailover.objects.filter(name='FO2').exists())

    def test_unresolved_primary_records_error(self):
        results = fresh_results()
        _import_failover({
            'name': 'FOx', 'primary_server': 'ghost.example.com',
            'secondary_server': 's.example.com',
        }, results)
        self.assertFalse(DHCPFailover.objects.filter(name='FOx').exists())
        self.assertEqual(len(results['failovers']['errors']), 1)

    def test_duplicate_name_skipped(self):
        DHCPFailover.objects.create(
            name='Dup', primary_server=self.primary, secondary_server=self.secondary,
        )
        results = fresh_results()
        _import_failover({
            'name': 'Dup', 'primary_server': 'p.example.com',
            'secondary_server': 's.example.com',
        }, results)
        self.assertEqual(DHCPFailover.objects.filter(name='Dup').count(), 1)
        self.assertEqual(len(results['failovers']['skipped']), 1)


class ImportScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = make_server()

    def _client(self):
        return FakePSUClient(
            scope_options={'10.0.1.0': [{'code': 66, 'value': '10.0.0.5', 'name': 'TFTP'}]},
            exclusions={'10.0.1.0': [FAKE_EXCLUSION]},
        )

    def test_snake_payload_creates_prefix_scope_options_exclusions(self):
        results = fresh_results()
        scope = _import_scope(self._client(), dict(FAKE_SCOPE_SNAKE), results, server=self.server)
        self.assertIsNotNone(scope)
        self.assertTrue(Prefix.objects.filter(prefix='10.0.1.0/24').exists())
        self.assertEqual(scope.name, 'Building A')
        self.assertEqual(scope.start_ip, '10.0.1.10')
        self.assertEqual(scope.end_ip, '10.0.1.254')
        self.assertEqual(scope.router, '10.0.1.1')
        self.assertEqual(scope.lease_lifetime, 86400)
        self.assertEqual(scope.server, self.server)
        self.assertEqual(scope.option_values.count(), 1)
        self.assertEqual(
            DHCPExclusionRange.objects.filter(scope=scope).count(), 1
        )

    def test_pascal_payload_equivalent(self):
        results = fresh_results()
        scope = _import_scope(self._client(), dict(FAKE_SCOPE_PASCAL), results, server=self.server)
        self.assertIsNotNone(scope)
        self.assertEqual(scope.name, 'Building A')
        self.assertEqual(scope.start_ip, '10.0.1.10')
        self.assertEqual(scope.router, '10.0.1.1')

    def test_router_zero_becomes_none(self):
        results = fresh_results()
        payload = dict(FAKE_SCOPE_SNAKE, router='0.0.0.0')
        scope = _import_scope(FakePSUClient(), payload, results, server=self.server)
        self.assertIsNone(scope.router)

    def test_create_missing_prefixes_disabled_and_absent(self):
        set_plugin_settings(create_missing_prefixes=False)
        results = fresh_results()
        scope = _import_scope(FakePSUClient(), dict(FAKE_SCOPE_SNAKE), results, server=self.server)
        self.assertIsNone(scope)
        self.assertFalse(Prefix.objects.filter(prefix='10.0.1.0/24').exists())
        self.assertEqual(len(results['scopes']['errors']), 1)

    def test_existing_scope_skipped_but_exclusions_imported(self):
        prefix = Prefix.objects.create(prefix='10.0.1.0/24', status='active')
        make_scope(name='Building A', prefix=prefix, server=self.server)
        results = fresh_results()
        scope = _import_scope(self._client(), dict(FAKE_SCOPE_SNAKE), results, server=self.server)
        self.assertEqual(len(results['scopes']['skipped']), 1)
        self.assertEqual(DHCPExclusionRange.objects.filter(scope=scope).count(), 1)


class RunImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = make_server()

    def test_run_import_creates_scope(self):
        fake = FakePSUClient(scopes=[dict(FAKE_SCOPE_SNAKE)], failover=[])
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            results = run_import(self.server)
        self.assertEqual(len(results['scopes']['created']), 1)
        self.assertTrue(DHCPScope.objects.filter(name='Building A').exists())
