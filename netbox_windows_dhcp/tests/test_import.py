"""
"Import from Server": failovers and scopes read from a server become NetBox objects, with
the right prefix (in the right VRF), router, options, exclusions and state. A
FakePSUClient supplies the server's data, so no network is used.
"""

from unittest import mock

from django.test import TestCase
from ipam.models import VRF, Prefix

from ..api_client import PSUClientError
from ..import_logic import _import_failover, _import_scope, run_import
from ..models import DHCPExclusionRange, DHCPFailover, DHCPScope
from .base import (
    PSU_CLIENT,
    fresh_results,
    make_failover,
    make_prefix,
    make_scope,
    make_server,
    set_plugin_settings,
)
from .fixtures import FAKE_EXCLUSION, FAKE_SCOPE_PASCAL, FAKE_SCOPE_SNAKE, FakePSUClient

# A scope-list record as a newer PSU script sends it (the router comes with the options).
REMOTE_NO_ROUTER = {k: v for k, v in FAKE_SCOPE_SNAKE.items() if k != 'router'}
LEASE_OPT = {'code': 51, 'value': ['86400'], 'vendor_class': ''}
ROUTER_OPT = {'code': 3, 'value': ['10.0.1.99'], 'vendor_class': ''}
OPTIONS_TIMEOUT = PSUClientError('simulated timeout', status_code=504)


class ImportFailoverTests(TestCase):

    def setUp(self):
        self.red = VRF.objects.create(name='Red')
        self.primary = make_server(name='P', hostname='p.example.com', default_scope_vrf=self.red)
        self.secondary = make_server(name='S', hostname='s.example.com')

    def _import(self, server=None, **payload):
        results = fresh_results()
        payload = {'name': 'FO', 'primary_server': 'p.example.com', 'secondary_server': 's.example.com',
                   **payload}
        _import_failover(payload, results, server=server or self.primary)
        return results

    def test_new_failover_is_created_in_maintenance_mode(self):
        results = self._import(mode='HotStandby')
        fo = DHCPFailover.objects.get(name='FO')
        self.assertEqual((fo.primary_server, fo.secondary_server, fo.mode),
                         (self.primary, self.secondary, 'HotStandby'))
        # The user checks its Default Scope VRF before its scopes come in.
        self.assertTrue(fo.maintenance_mode)
        self.assertIsNotNone(fo.maintenance_enabled_at)
        self.assertTrue(fo.sync_enabled)
        self.assertEqual(fo.default_scope_vrf, self.red)  # copied from the importing server
        self.assertEqual(results['failovers']['created'], ['FO'])
        self.assertIn('FO', results['failovers']['maintenance'][0])

    def test_pascal_case_keys(self):
        _import_failover({'Name': 'FO2', 'PrimaryServer': 'p.example.com', 'SecondaryServer': 's.example.com',
                          'Mode': 'LoadBalance'}, fresh_results(), server=self.primary)
        self.assertTrue(DHCPFailover.objects.filter(name='FO2').exists())

    def test_unknown_partner_is_an_error(self):
        results = self._import(primary_server='ghost.example.com')
        self.assertFalse(DHCPFailover.objects.exists())
        self.assertEqual(len(results['failovers']['errors']), 1)

    def test_same_name_on_another_server_pair_is_imported(self):
        make_failover(name='FO', primary=make_server(name='X', hostname='x'),
                      secondary=make_server(name='Y', hostname='y'))
        results = self._import()
        self.assertEqual(DHCPFailover.objects.filter(name='FO').count(), 2)
        self.assertEqual(results['failovers']['created'], ['FO'])

    def test_existing_for_this_server_is_skipped(self):
        make_failover(name='FO', primary=self.primary, secondary=self.secondary)
        results = self._import(server=self.secondary)
        self.assertEqual(DHCPFailover.objects.filter(name='FO').count(), 1)
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

    def _import(self, payload, client=None, server=None):
        results = fresh_results()
        scope = _import_scope(client or FakePSUClient(), dict(payload), results, server=server or self.server)
        return scope, results

    def test_creates_prefix_scope_options_and_exclusions(self):
        scope, _ = self._import(dict(FAKE_SCOPE_SNAKE, description='Lobby'), client=self._client())
        self.assertTrue(Prefix.objects.filter(prefix='10.0.1.0/24').exists())
        self.assertEqual(
            (scope.name, scope.description, scope.start_ip, scope.end_ip, scope.router, scope.lease_lifetime),
            ('Building A', 'Lobby', '10.0.1.10', '10.0.1.254', '10.0.1.1', 86400),
        )
        self.assertEqual(scope.server, self.server)
        self.assertEqual(scope.option_values.count(), 1)
        self.assertEqual(DHCPExclusionRange.objects.filter(scope=scope).count(), 1)

    def test_pascal_case_payload(self):
        scope, _ = self._import(FAKE_SCOPE_PASCAL, client=self._client())
        self.assertEqual((scope.name, scope.start_ip, scope.router), ('Building A', '10.0.1.10', '10.0.1.1'))

    def test_router_zero_means_none(self):
        scope, _ = self._import(dict(FAKE_SCOPE_SNAKE, router='0.0.0.0'))
        self.assertIsNone(scope.router)

    def test_no_prefix_when_create_missing_prefixes_is_off(self):
        # The scope still comes in, with no prefix (it lands on Unassigned Scopes).
        set_plugin_settings(create_missing_prefixes=False)
        scope, results = self._import(FAKE_SCOPE_SNAKE)
        self.assertIsNone(scope.prefix)
        self.assertEqual((scope.network, scope.prefix_length), ('10.0.1.0', 24))
        self.assertFalse(Prefix.objects.filter(prefix='10.0.1.0/24').exists())
        self.assertEqual(results['scopes']['errors'], [])
        self.assertEqual(len(results['scopes']['unassigned']), 1)

    def test_scope_in_a_failover_netbox_does_not_have_is_skipped(self):
        # Never imported as a standalone scope: with push on, the next sync would pull it
        # out of its failover on the server.
        scope, results = self._import(dict(FAKE_SCOPE_SNAKE, failover_name='FO-Ghost'))
        self.assertIsNone(scope)
        self.assertFalse(DHCPScope.objects.exists())
        self.assertEqual(len(results['scopes']['errors']), 1)

    def test_ambiguous_failover_skips_the_scope(self):
        make_failover(name='FO', primary=self.server, secondary=make_server(name='B', hostname='b'))
        make_failover(name='FO', primary=self.server, secondary=make_server(name='C', hostname='c'))
        scope, results = self._import(dict(FAKE_SCOPE_SNAKE, failover_name='FO'))
        self.assertIsNone(scope)
        self.assertEqual(len(results['scopes']['errors']), 1)


class ImportRouterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.server = make_server()

    def test_router_comes_from_the_options(self):
        fake = FakePSUClient(scope_options={'10.0.1.0': [LEASE_OPT, ROUTER_OPT]})
        scope = _import_scope(fake, dict(REMOTE_NO_ROUTER), fresh_results(), server=self.server)
        self.assertEqual(scope.router, '10.0.1.99')
        # Options 3 and 51 are scope fields, not option values.
        self.assertEqual(scope.option_values.count(), 0)

    def test_failed_options_read_skips_the_scope(self):
        results = fresh_results()
        scope = _import_scope(FakePSUClient(scope_options_error=OPTIONS_TIMEOUT), dict(REMOTE_NO_ROUTER),
                              results, server=self.server)
        self.assertIsNone(scope)
        self.assertFalse(DHCPScope.objects.exists())
        self.assertEqual(len(results['scopes']['errors']), 1)

    def test_older_script_keeps_importing_when_options_fail(self):
        # An older script still sends the router in the scope list.
        results = fresh_results()
        scope = _import_scope(FakePSUClient(scope_options_error=OPTIONS_TIMEOUT), dict(FAKE_SCOPE_SNAKE),
                              results, server=self.server)
        self.assertEqual(scope.router, '10.0.1.1')
        self.assertEqual(len(results['option_values']['errors']), 1)


class ImportPrefixTests(TestCase):
    """Imported scopes use a prefix in their server's (or failover's) Default Scope VRF."""

    def setUp(self):
        self.red = VRF.objects.create(name='Red')
        self.server = make_server(default_scope_vrf=self.red)

    def _import(self, payload=FAKE_SCOPE_SNAKE, server=None):
        results = fresh_results()
        return _import_scope(FakePSUClient(), dict(payload), results, server=server or self.server), results

    def test_uses_the_prefix_in_the_default_vrf(self):
        make_prefix('10.0.1.0/24')  # global — ignored
        red_prefix = make_prefix('10.0.1.0/24', vrf=self.red)
        scope, _ = self._import()
        self.assertEqual(scope.prefix, red_prefix)

    def test_creates_a_missing_prefix_in_the_default_vrf(self):
        make_prefix('10.0.1.0/24')  # global — not used
        scope, _ = self._import()
        self.assertEqual(scope.prefix.vrf, self.red)
        self.assertEqual(Prefix.objects.filter(prefix='10.0.1.0/24').count(), 2)

    def test_blank_default_means_global(self):
        scope, _ = self._import(server=make_server(name='Global', hostname='global'))
        self.assertIsNone(scope.prefix.vrf)

    def test_failover_scope_uses_the_failover_vrf(self):
        blue = VRF.objects.create(name='Blue')
        make_failover(name='FO', primary=self.server, default_scope_vrf=blue)
        scope, _ = self._import(dict(FAKE_SCOPE_SNAKE, failover_name='FO'))
        self.assertEqual(scope.prefix.vrf, blue)
        self.assertIsNone(scope.server)

    def test_prefix_taken_by_another_scope_means_no_prefix(self):
        prefix = make_prefix('10.0.1.0/24', vrf=self.red)
        make_scope(name='Prod scope', prefix=prefix, server=make_server(name='Prod', hostname='prod'))
        scope, results = self._import()
        self.assertIsNone(scope.prefix)
        self.assertEqual(scope.network, '10.0.1.0')
        self.assertIn('Prod scope', results['scopes']['unassigned'][0])

    def test_two_matching_prefixes_means_no_prefix(self):
        Prefix.objects.create(prefix='10.0.1.0/24', vrf=self.red, status='active')
        Prefix.objects.create(prefix='10.0.1.0/24', vrf=self.red, status='active')
        scope, results = self._import()
        self.assertIsNone(scope.prefix)
        self.assertEqual(len(results['scopes']['unassigned']), 1)


class ImportExistingScopeTests(TestCase):
    """A scope already in NetBox is matched by its server and network, never by name."""

    def setUp(self):
        self.server = make_server()

    def _import(self):
        return _import_scope(FakePSUClient(exclusions={'10.0.1.0': [FAKE_EXCLUSION]}), dict(FAKE_SCOPE_SNAKE),
                             fresh_results(), server=self.server)

    def test_matched_by_server_and_network_and_its_exclusions_imported(self):
        existing = make_scope(name='Renamed on Windows', prefix=make_prefix('10.0.1.0/24'), server=self.server)
        self.assertEqual(self._import(), existing)
        self.assertEqual(DHCPScope.objects.count(), 1)
        self.assertEqual(DHCPExclusionRange.objects.filter(scope=existing).count(), 1)

    def test_matched_through_a_failover_the_server_is_in(self):
        failover = make_failover(primary=make_server(name='P', hostname='p'), secondary=self.server)
        existing = make_scope(prefix=make_prefix('10.0.1.0/24'), failover=failover)
        self.assertEqual(self._import(), existing)

    def test_another_servers_scope_doesnt_count(self):
        make_scope(name='Prod', prefix=make_prefix('10.0.1.0/24'), server=make_server(name='Prod', hostname='prod'))
        scope = self._import()
        self.assertEqual(scope.server, self.server)
        self.assertIsNone(scope.prefix)  # the prefix belongs to Prod's scope
        self.assertEqual(DHCPScope.objects.count(), 2)


class RunImportTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.server = make_server()
        cls.partner = make_server(name='Partner', hostname='partner.example.com')

    def _run(self, fake):
        with mock.patch(PSU_CLIENT, return_value=fake):
            return run_import(self.server)

    def test_every_scope_is_imported_with_its_state(self):
        results = self._run(FakePSUClient(scopes=[
            dict(FAKE_SCOPE_SNAKE, state='Inactive'),
            dict(FAKE_SCOPE_SNAKE, scope_id='10.0.2.0', name='Building B', state='Active',
                 start_ip='10.0.2.10', end_ip='10.0.2.254', router='10.0.2.1'),
            dict(FAKE_SCOPE_SNAKE, scope_id='10.0.3.0', name='Building C',
                 start_ip='10.0.3.10', end_ip='10.0.3.254', router='10.0.3.1'),
        ], failover=[]))
        self.assertEqual(len(results['scopes']['created']), 3)
        # No state reported counts as active.
        self.assertEqual(dict(DHCPScope.objects.values_list('network', 'active')),
                         {'10.0.1.0': False, '10.0.2.0': True, '10.0.3.0': True})

    def test_scopes_of_a_new_failover_wait(self):
        results = self._run(FakePSUClient(
            scopes=[
                dict(FAKE_SCOPE_SNAKE, failover_name='FO-New'),
                dict(FAKE_SCOPE_SNAKE, scope_id='10.0.2.0', name='Standalone',
                     start_ip='10.0.2.10', end_ip='10.0.2.254', router='10.0.2.1'),
            ],
            failover=[{'name': 'FO-New', 'primary_server': self.server.hostname,
                       'secondary_server': self.partner.hostname}],
        ))
        self.assertTrue(DHCPFailover.objects.get(name='FO-New').maintenance_mode)
        self.assertEqual(list(DHCPScope.objects.values_list('name', flat=True)), ['Standalone'])
        self.assertEqual(len(results['scopes']['skipped']), 1)

    def test_scopes_of_an_existing_failover_are_imported(self):
        # The second run, after the user checked the VRF and ended maintenance.
        failover = make_failover(name='FO-Old', primary=self.server, secondary=self.partner)
        self._run(FakePSUClient(
            scopes=[dict(FAKE_SCOPE_SNAKE, failover_name='FO-Old')],
            failover=[{'name': 'FO-Old', 'primary_server': self.server.hostname,
                       'secondary_server': self.partner.hostname}],
        ))
        self.assertEqual(DHCPScope.objects.get().failover, failover)
