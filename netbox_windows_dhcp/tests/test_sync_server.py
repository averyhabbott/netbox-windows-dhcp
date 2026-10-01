"""
A whole server sync: fetch failures, read-only servers, the health check, failover
replication, and which server handles which scope.
"""

from contextlib import contextmanager
from unittest import mock

from core.exceptions import JobFailed
from core.models import ObjectChange
from django.db import transaction
from django.test import TestCase
from ipam.models import IPAddress, VRF
from netbox.context import current_request
from utilities.testing import TestCase as NetBoxTestCase

from ..api_client import PSUClientError
from ..background_tasks import (
    _change_logging,
    _check_server_health,
    _pull_scope_failover,
    _sync_scope_ips,
    _sync_server,
)
from ..constants import PSU_SCRIPT_VERSION
from ..models import DHCPExclusionRange, DHCPLeaseInfo, DHCPScope, DHCPServer
from .base import (
    NULL_LOGGER,
    PSU_CLIENT,
    get_ip,
    make_failover,
    make_option_definition,
    make_option_value,
    make_prefix,
    make_scope,
    make_server,
    run_sync,
    make_unassigned_scope,
    set_plugin_settings,
)
from .fixtures import FAKE_LEASE, FAKE_RESERVATION, FAKE_SCOPE_SNAKE, FakePSUClient


class SyncServerBulkFetchTests(TestCase):
    """
    _sync_server fetches leases/reservations once per server (bulk mode, no scope_id)
    instead of once per scope. These tests cover the behavior that has to be preserved
    across that change: per-scope grouping, atomic-pair failure handling (a fetch
    failure must never look like "confirmed empty" and drive a deletion), and
    push_reservations sharing the same bulk reservations fetch.
    """

    @classmethod
    def setUpTestData(cls):
        set_plugin_settings(sync_ip_addresses=True, push_scope_info=False)
        cls.server = make_server()
        cls.scope_a = make_scope(
            name='Building A', prefix=make_prefix('10.0.1.0/24'), server=cls.server,
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )
        cls.scope_b = make_scope(
            name='Building B', prefix=make_prefix('10.0.2.0/24'), server=cls.server,
            start_ip='10.0.2.10', end_ip='10.0.2.254', router='10.0.2.1',
        )

    def _remote_scopes(self):
        return [
            dict(FAKE_SCOPE_SNAKE),
            dict(FAKE_SCOPE_SNAKE, scope_id='10.0.2.0', name='Building B'),
        ]

    def test_bulk_response_is_grouped_by_scope_id(self):
        fake = FakePSUClient(
            scopes=self._remote_scopes(),
            leases={
                '10.0.1.0': [dict(FAKE_LEASE)],
                '10.0.2.0': [dict(FAKE_LEASE, ip_address='10.0.2.50', scope_id='10.0.2.0')],
            },
            reservations={'10.0.1.0': [dict(FAKE_RESERVATION)]},
            exclusions={'10.0.1.0': [], '10.0.2.0': []},
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.server,
                sync_ip_addresses=True, push_reservations=False, push_scope_info=False,
            )
        # Bulk mode: exactly one call each, no scope_id filter.
        self.assertEqual(fake.list_leases_calls, [None])
        self.assertEqual(fake.list_reservations_calls, [None])
        self.assertEqual(get_ip('10.0.1.50').status, 'dhcp')
        self.assertEqual(get_ip('10.0.2.50').status, 'dhcp')
        self.assertEqual(get_ip('10.0.1.100').status, 'reserved')

    def test_failed_leases_fetch_skips_cleanup_for_every_scope_without_deleting(self):
        stale_a = IPAddress.objects.create(address='10.0.1.50/24', status='dhcp')
        stale_b = IPAddress.objects.create(address='10.0.2.50/24', status='dhcp')
        fake = FakePSUClient(
            scopes=self._remote_scopes(),
            reservations={},
            exclusions={'10.0.1.0': [], '10.0.2.0': []},
            leases_error=PSUClientError('simulated timeout', status_code=504),
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.server,
                sync_ip_addresses=True, push_reservations=False, push_scope_info=False,
            )
        # Would normally be deleted (no lease data says they still exist) — but a failed
        # fetch must never be treated as "confirmed gone."
        stale_a.refresh_from_db()
        stale_b.refresh_from_db()
        self.assertEqual(stale_a.status, 'dhcp')
        self.assertEqual(stale_b.status, 'dhcp')

    def test_failed_reservations_fetch_also_skips_cleanup_even_though_leases_succeeded(self):
        stale_a = IPAddress.objects.create(address='10.0.1.50/24', status='dhcp')
        fake = FakePSUClient(
            scopes=self._remote_scopes(),
            leases={'10.0.1.0': [dict(FAKE_LEASE)]},
            exclusions={'10.0.1.0': [], '10.0.2.0': []},
            reservations_error=PSUClientError('simulated timeout', status_code=504),
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.server,
                sync_ip_addresses=True, push_reservations=False, push_scope_info=False,
            )
        # Leases fetch succeeded, but cleanup needs both lists together — a reservations
        # failure must still block cleanup, not just leave reservation-derived state stale.
        stale_a.refresh_from_db()
        self.assertEqual(stale_a.status, 'dhcp')  # untouched, not deleted

    def test_failed_reservations_fetch_skips_push_without_attempting_writes(self):
        ip = IPAddress.objects.create(address='10.0.1.100/24', status='reserved')
        ip.custom_field_data['dhcp_client_id'] = 'aa-bb-cc-dd-ee-ff'
        ip.save()
        fake = FakePSUClient(
            scopes=self._remote_scopes(),
            leases={'10.0.1.0': [], '10.0.2.0': []},
            exclusions={'10.0.1.0': [], '10.0.2.0': []},
            reservations_error=PSUClientError('simulated timeout', status_code=504),
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.server,
                sync_ip_addresses=True, push_reservations=True, push_scope_info=False,
            )
        self.assertEqual(fake.created_reservations, [])

    def test_leases_only_failure_does_not_block_reservation_push(self):
        ip = IPAddress.objects.create(address='10.0.1.150/24', status='reserved')
        ip.custom_field_data['dhcp_client_id'] = '11-22-33-44-55-66'
        ip.save()
        fake = FakePSUClient(
            scopes=self._remote_scopes(),
            reservations={'10.0.1.0': [], '10.0.2.0': []},
            exclusions={'10.0.1.0': [], '10.0.2.0': []},
            leases_error=PSUClientError('simulated timeout', status_code=504),
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.server,
                sync_ip_addresses=True, push_reservations=True, push_scope_info=False,
            )
        # Reservations fetch succeeded, so the push should still go through even though
        # the (unrelated) bulk leases fetch failed.
        self.assertEqual(len(fake.created_reservations), 1)
        self.assertEqual(fake.created_reservations[0]['ip_address'], '10.0.1.150')

    def test_inactive_scope_is_synced_like_any_other(self):
        # An inactive scope is matched and synced like an active one: its state is pulled,
        # and with a prefix its leases are synced too (lease work follows the prefix and
        # maintenance mode, not the scope's state).
        fake = FakePSUClient(
            scopes=[
                dict(FAKE_SCOPE_SNAKE, state='Active'),
                dict(FAKE_SCOPE_SNAKE, scope_id='10.0.2.0', name='Building B', state='Inactive',
                     start_ip='10.0.2.10', end_ip='10.0.2.254', router='10.0.2.1'),
            ],
            leases={
                '10.0.1.0': [dict(FAKE_LEASE)],
                '10.0.2.0': [dict(FAKE_LEASE, ip_address='10.0.2.50', scope_id='10.0.2.0')],
            },
            reservations={'10.0.1.0': [], '10.0.2.0': []},
            exclusions={'10.0.1.0': [], '10.0.2.0': []},
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.server,
                sync_ip_addresses=True, push_reservations=False, push_scope_info=False,
            )
        self.assertEqual(fake.list_scopes_calls, [False])
        self.assertIsNotNone(get_ip('10.0.1.50'))
        self.assertIsNotNone(get_ip('10.0.2.50'))
        self.scope_a.refresh_from_db()
        self.scope_b.refresh_from_db()
        self.assertTrue(self.scope_a.active)
        self.assertFalse(self.scope_b.active)

    def test_failed_exclusions_fetch_leaves_existing_exclusions_intact(self):
        from ..models import DHCPExclusionRange
        ex = DHCPExclusionRange.objects.create(
            scope=self.scope_a, start_ip='10.0.1.200', end_ip='10.0.1.210',
        )
        fake = FakePSUClient(
            scopes=self._remote_scopes(),
            leases={'10.0.1.0': [], '10.0.2.0': []},
            reservations={'10.0.1.0': [], '10.0.2.0': []},
            exclusions_error=PSUClientError('bulk exclusions fetch failed', status_code=500),
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.server,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=False,
            )
        # Failed fetch must never be treated as "confirmed empty" — existing exclusion preserved.
        self.assertTrue(DHCPExclusionRange.objects.filter(pk=ex.pk).exists())

    def test_failed_options_fetch_leaves_existing_options_intact(self):
        opt_def = make_option_definition(code=205, name='Test Option 205')
        opt_val = make_option_value(option_definition=opt_def, value='kept')
        self.scope_a.option_values.add(opt_val)
        fake = FakePSUClient(
            scopes=self._remote_scopes(),
            leases={'10.0.1.0': [], '10.0.2.0': []},
            reservations={'10.0.1.0': [], '10.0.2.0': []},
            exclusions={'10.0.1.0': [], '10.0.2.0': []},
            options_error=PSUClientError('bulk options fetch failed', status_code=500),
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.server,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=False,
            )
        # Failed fetch must never be treated as "confirmed empty" — existing option preserved.
        self.assertTrue(self.scope_a.option_values.filter(pk=opt_val.pk).exists())


class LeaseActiveFailedFetchTests(NetBoxTestCase):

    def setUp(self):
        super().setUp()
        self.scope = make_scope(prefix=make_prefix('10.0.1.0/24'))

    def _run(self, leases=(), reservations=(), **kwargs):
        _sync_scope_ips(NULL_LOGGER, self.scope, leases=list(leases), reservations=list(reservations), **kwargs)

    def _info(self, ip_str):
        return DHCPLeaseInfo.objects.get(ip_address__address__net_host=ip_str)

    def test_failed_fetch_changes_nothing(self):
        set_plugin_settings(sync_ip_addresses=True, push_scope_info=False)
        ip = IPAddress.objects.create(address='10.0.1.60/24', status='dhcp')
        DHCPLeaseInfo.objects.create(ip_address=ip, lease_hostname='kept', active=True)
        fake = FakePSUClient(
            scopes=[dict(FAKE_SCOPE_SNAKE)], reservations={}, exclusions={'10.0.1.0': []},
            leases_error=PSUClientError('simulated timeout', status_code=504),
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(NULL_LOGGER, self.scope.server,
                         sync_ip_addresses=True, push_reservations=False, push_scope_info=False)
        self.assertTrue(self._info('10.0.1.60').active)


class ScopeBeforeCleanupOrderTests(TestCase):
    """_sync_server pulls the scope's range and exclusions before IP cleanup, so a range
    change on the server is applied in the same run."""

    def test_cleanup_uses_the_range_just_pulled_from_the_server(self):
        server = make_server()
        scope = make_scope(name='Building A', prefix=make_prefix('10.0.1.0/24'), server=server,
                           start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1')
        # Hand-made IP inside NetBox's old range, outside the server's new one.
        ip = IPAddress.objects.create(address='10.0.1.200/24', status='active')
        fake = FakePSUClient(
            scopes=[dict(FAKE_SCOPE_SNAKE, end_ip='10.0.1.100')],
            leases={'10.0.1.0': []}, reservations={'10.0.1.0': []},
            exclusions={'10.0.1.0': []}, scope_options={'10.0.1.0': []},
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(NULL_LOGGER, server, sync_ip_addresses=True,
                         push_reservations=False, push_scope_info=False)
        scope.refresh_from_db()
        self.assertEqual(scope.end_ip, '10.0.1.100')
        self.assertTrue(IPAddress.objects.filter(pk=ip.pk).exists())


class SyncServerAccessLevelSkipTests(TestCase):
    """_sync_server must proactively skip pushing to a known-read-only server
    instead of letting every push attempt reactively 403."""

    @classmethod
    def setUpTestData(cls):
        set_plugin_settings(sync_ip_addresses=True, push_scope_info=True, push_reservations=True)
        cls.server = make_server()
        cls.prefix = make_prefix('10.0.1.0/24')
        cls.scope = make_scope(
            name='Building A', prefix=cls.prefix, server=cls.server,
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )

    def test_read_only_server_pulls_but_never_pushes(self):
        self.server.access_level = DHCPServer.ACCESS_RO
        self.server.save(update_fields=['access_level'])
        # A locally-reserved IP with no matching remote reservation — would be
        # pushed as a new reservation if the push weren't skipped.
        ip = IPAddress.objects.create(address='10.0.1.150/24', status='reserved')
        ip.custom_field_data['dhcp_client_id'] = 'aa-bb-cc-dd-ee-ff'
        ip.save()
        remote = dict(FAKE_SCOPE_SNAKE, router='10.0.1.99')  # differs, would normally trigger a scope push
        fake = FakePSUClient(
            scopes=[remote],
            leases={'10.0.1.0': [dict(FAKE_LEASE)]},
            reservations={'10.0.1.0': []},
        )

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.server,
                sync_ip_addresses=True, push_reservations=True, push_scope_info=True,
            )

        # Pull still happened.
        self.assertIsNotNone(get_ip('10.0.1.50'))
        # No push was attempted against the read-only server.
        self.assertEqual(fake.updated_scopes, [])
        self.assertEqual(fake.created_reservations, [])

    def test_unknown_access_level_falls_back_to_reactive_attempt(self):
        self.assertEqual(self.server.access_level, DHCPServer.ACCESS_UNKNOWN)
        ip = IPAddress.objects.create(address='10.0.1.150/24', status='reserved')
        ip.custom_field_data['dhcp_client_id'] = 'aa-bb-cc-dd-ee-ff'
        ip.save()
        remote = dict(FAKE_SCOPE_SNAKE, router='10.0.1.99')
        fake = FakePSUClient(
            scopes=[remote],
            reservations={'10.0.1.0': []},
            scope_options={'10.0.1.0': []},
            exclusions={'10.0.1.0': []},
        )

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.server,
                sync_ip_addresses=False, push_reservations=True, push_scope_info=True,
            )

        # access_level unknown → old reactive behavior: the push is still attempted.
        self.assertEqual(len(fake.updated_scopes), 1)
        self.assertEqual(len(fake.created_reservations), 1)
        self.assertEqual(fake.created_reservations[0]['ip_address'], '10.0.1.150')


class CheckServerHealthTests(TestCase):
    """_check_server_health — shared by DHCPServerSyncJob and DHCPScopePushJob
    so both keep DHCPServer's health bookkeeping current and both fail loudly
    (raise JobFailed) rather than completing silently when unreachable."""

    def test_success_updates_health_fields(self):
        server = make_server()
        fake = FakePSUClient()  # default health => the current PSU_SCRIPT_VERSION
        job = mock.Mock()

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _check_server_health(NULL_LOGGER, job, server)

        server.refresh_from_db()
        self.assertEqual(server.health_status, DHCPServer.HEALTH_HEALTHY)
        self.assertEqual(server.health_error, '')
        self.assertEqual(server.psu_script_version, PSU_SCRIPT_VERSION)
        self.assertEqual(server.access_level, DHCPServer.ACCESS_RW)  # ping_write() succeeds by default
        self.assertIsNotNone(server.last_health_check)

    def test_old_psu_script_is_warned_about_but_still_healthy(self):
        for version, warned in (('1.1.2', True), (PSU_SCRIPT_VERSION, False), ('', False)):
            with self.subTest(version=version or 'unknown'):
                server = make_server(name=f'S {version}', hostname=f's{version}.example.com')
                logger = mock.Mock()
                with mock.patch(PSU_CLIENT, return_value=FakePSUClient(health={'version': version})):
                    _check_server_health(logger, mock.Mock(), server)
                server.refresh_from_db()
                self.assertEqual(server.health_status, DHCPServer.HEALTH_HEALTHY)
                warnings = [c.args[0] for c in logger.warning.call_args_list]
                self.assertEqual(any(version in w and PSU_SCRIPT_VERSION in w for w in warnings), warned)
                self.assertEqual(len(warnings), int(warned))

    def test_failure_marks_unreachable_and_raises_job_failed(self):
        server = make_server()
        job = mock.Mock()

        class BoomClient:
            def ping_read(self):
                raise PSUClientError('connection refused')

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=BoomClient()):
            with self.assertRaises(JobFailed):
                _check_server_health(NULL_LOGGER, job, server)

        server.refresh_from_db()
        self.assertEqual(server.health_status, DHCPServer.HEALTH_UNREACHABLE)
        self.assertEqual(server.access_level, DHCPServer.ACCESS_UNKNOWN)
        self.assertIn('connection refused', server.health_error)
        self.assertIn(server.name, job.error)

    def test_readonly_token_sets_access_level_ro_and_stays_healthy(self):
        server = make_server()
        fake = FakePSUClient(ping_write_error=PSUClientError('forbidden', status_code=403))
        job = mock.Mock()

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _check_server_health(NULL_LOGGER, job, server)  # must not raise

        server.refresh_from_db()
        self.assertEqual(server.health_status, DHCPServer.HEALTH_HEALTHY)
        self.assertEqual(server.access_level, DHCPServer.ACCESS_RO)

    def test_ping_write_non_403_failure_marks_unreachable_and_raises_job_failed(self):
        server = make_server()
        fake = FakePSUClient(ping_write_error=PSUClientError('service unavailable', status_code=503))
        job = mock.Mock()

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            with self.assertRaises(JobFailed):
                _check_server_health(NULL_LOGGER, job, server)

        server.refresh_from_db()
        self.assertEqual(server.health_status, DHCPServer.HEALTH_UNREACHABLE)
        self.assertEqual(server.access_level, DHCPServer.ACCESS_UNKNOWN)
        self.assertIn('service unavailable', server.health_error)


class SyncServerFailoverReplicationTests(TestCase):
    """_sync_server — batches Invoke-DhcpServerv4FailoverReplication into a
    single POST /api/dhcp/failover/replicate call per server, covering every
    scope that actually changed during the run (attrs, options, or exclusions),
    rather than one expensive replication call per scope."""

    def setUp(self):
        self.primary = make_server(name='Primary', hostname='primary.example.com')
        self.secondary = make_server(name='Secondary', hostname='secondary.example.com')
        self.failover = make_failover(name='FO-A', primary=self.primary, secondary=self.secondary)

    def test_replicates_once_for_multiple_changed_scopes(self):
        prefix1 = make_prefix('10.0.1.0/24')
        prefix2 = make_prefix('10.0.2.0/24')
        make_scope(
            name='A', prefix=prefix1, failover=self.failover,
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )
        make_scope(
            name='B', prefix=prefix2, failover=self.failover,
            start_ip='10.0.2.10', end_ip='10.0.2.254', router='10.0.2.1',
        )
        # Remote scopes report a stale router so _push_scope finds a diff and pushes.
        remote1 = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='A', router='10.0.1.99', failover_name='FO-A')
        remote2 = dict(
            FAKE_SCOPE_SNAKE, scope_id='10.0.2.0', name='B',
            start_ip='10.0.2.10', end_ip='10.0.2.254', router='10.0.2.99', failover_name='FO-A',
        )
        fake = FakePSUClient(
            scopes=[remote1, remote2],
            scope_options={'10.0.1.0': [], '10.0.2.0': []},
            exclusions={'10.0.1.0': [], '10.0.2.0': []},
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.primary,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=True,
            )

        self.assertEqual(len(fake.replicated_failover_calls), 1)
        self.assertEqual(sorted(fake.replicated_failover_calls[0]), ['10.0.1.0', '10.0.2.0'])

    def test_no_replication_when_nothing_changed(self):
        prefix = make_prefix('10.0.1.0/24')
        make_scope(
            name='A', prefix=prefix, failover=self.failover,
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )
        remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='A', failover_name='FO-A')
        fake = FakePSUClient(
            scopes=[remote],
            scope_options={'10.0.1.0': []},
            exclusions={'10.0.1.0': []},
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.primary,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=True,
            )

        self.assertEqual(fake.replicated_failover_calls, [])

    def test_exclusion_only_change_still_triggers_replication(self):
        # Attributes/options match exactly, but an exclusion range differs —
        # replication must still fire (an exclusion-only diff must not be
        # invisible to the "did anything change?" gate).
        prefix = make_prefix('10.0.1.0/24')
        scope = make_scope(
            name='A', prefix=prefix, failover=self.failover,
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )
        DHCPExclusionRange.objects.create(scope=scope, start_ip='10.0.1.200', end_ip='10.0.1.210')
        remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='A', failover_name='FO-A')
        fake = FakePSUClient(
            scopes=[remote],
            scope_options={'10.0.1.0': []},
            exclusions={'10.0.1.0': []},  # server has none yet -> gets created
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.primary,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=True,
            )

        self.assertEqual(fake.updated_scopes, [])  # confirm attrs/options truly didn't change
        self.assertEqual(len(fake.replicated_failover_calls), 1)
        self.assertEqual(fake.replicated_failover_calls[0], ['10.0.1.0'])

    def test_standalone_scope_change_does_not_trigger_replication(self):
        server = make_server(name='Standalone', hostname='standalone.example.com')
        prefix = make_prefix('10.0.3.0/24')
        make_scope(
            name='C', prefix=prefix, server=server,
            start_ip='10.0.3.10', end_ip='10.0.3.254', router='10.0.3.1',
        )
        remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.3.0', name='C', router='10.0.3.99')
        fake = FakePSUClient(
            scopes=[remote],
            scope_options={'10.0.3.0': []},
            exclusions={'10.0.3.0': []},
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, server,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=True,
            )

        self.assertEqual(len(fake.updated_scopes), 1)  # confirm the push did happen
        self.assertEqual(fake.replicated_failover_calls, [])


class SyncServerTiesTests(TestCase):

    def setUp(self):
        set_plugin_settings(sync_ip_addresses=False, push_reservations=False, push_scope_info=False)
        self.dev = make_server(name='Dev', hostname='dev')
        self.prod = make_server(name='Prod', hostname='prod')
        red = VRF.objects.create(name='Red')
        self.dev_scope = make_scope(name='Dev scope', prefix=make_prefix('10.0.1.0/24', vrf=red), server=self.dev)
        self.prod_scope = make_scope(name='Prod scope', prefix=make_prefix('10.0.1.0/24'), server=self.prod)

    def _sync(self, server, fake, push_scope_info=False):
        run_sync(server, fake, push_scope_info=push_scope_info)
        return fake

    def test_each_server_syncs_only_its_own_scope(self):
        self._sync(self.dev, FakePSUClient(scopes=[dict(FAKE_SCOPE_SNAKE, name='Dev renamed')]))
        self._sync(self.prod, FakePSUClient(scopes=[dict(FAKE_SCOPE_SNAKE, name='Prod renamed')]))
        self.dev_scope.refresh_from_db()
        self.prod_scope.refresh_from_db()
        self.assertEqual((self.dev_scope.name, self.prod_scope.name), ('Dev renamed', 'Prod renamed'))

    def test_push_uses_the_servers_own_scope(self):
        fake = self._sync(self.prod, FakePSUClient(scopes=[dict(FAKE_SCOPE_SNAKE, name='Windows name')]),
                          push_scope_info=True)
        self.assertEqual(fake.updated_scopes[0][1]['name'], 'Prod scope')
        self.assertEqual(fake.deleted_scopes, [])

    def test_two_scopes_with_one_network_on_a_server_are_skipped(self):
        make_scope(name='Dev duplicate', prefix=make_prefix('10.0.1.0/24', vrf=VRF.objects.create(name='Blue')),
                   server=self.dev)
        for push in (True, False):
            with self.subTest(push_scope_info=push):
                fake = self._sync(self.dev, FakePSUClient(scopes=[dict(FAKE_SCOPE_SNAKE, name='X')]),
                                  push_scope_info=push)
                self.assertEqual((fake.updated_scopes, fake.deleted_scopes, fake.created_scopes), ([], [], []))
                self.assertEqual(DHCPScope.objects.filter(network='10.0.1.0').count(), 3)
                self.assertFalse(DHCPScope.objects.filter(name='X').exists())

    def test_pull_failover_ambiguous_leaves_scope_alone(self):
        make_failover(name='FO', primary=self.dev, secondary=make_server(name='B', hostname='b'))
        make_failover(name='FO', primary=self.dev, secondary=make_server(name='C', hostname='c'))
        _pull_scope_failover(NULL_LOGGER, self.dev_scope, {'failover_name': 'FO'}, self.dev)
        self.dev_scope.refresh_from_db()
        self.assertEqual((self.dev_scope.server, self.dev_scope.failover), (self.dev, None))


class SyncWithoutPrefixTests(TestCase):

    def setUp(self):
        set_plugin_settings(sync_ip_addresses=True, push_reservations=False, push_scope_info=False)
        self.server = make_server()

    def _sync(self, fake, **kwargs):
        run_sync(self.server, fake, **{'sync_ip_addresses': True, **kwargs})
        return fake

    def test_pull_updates_settings_but_no_ips(self):
        scope = make_unassigned_scope(name='Old name', server=self.server)
        remote = dict(FAKE_SCOPE_SNAKE, name='New name')
        self._sync(FakePSUClient(
            scopes=[remote], leases={'10.0.1.0': [FAKE_LEASE]},
            reservations={'10.0.1.0': [FAKE_RESERVATION]},
            exclusions={'10.0.1.0': [{'start_ip': '10.0.1.200', 'end_ip': '10.0.1.210'}]},
        ))
        scope.refresh_from_db()
        self.assertEqual(scope.name, 'New name')
        self.assertEqual(scope.exclusion_ranges.count(), 1)
        self.assertFalse(IPAddress.objects.exists())

    def test_push_creates_a_missing_scope_but_no_reservations(self):
        make_unassigned_scope(server=self.server, network='10.0.4.0', prefix_length=22,
                              start_ip='10.0.4.10', end_ip='10.0.7.200')
        fake = self._sync(FakePSUClient(scopes=[]), push_scope_info=True, push_reservations=True)
        self.assertEqual(len(fake.created_scopes), 1)
        self.assertEqual(fake.created_scopes[0]['scope_id'], '10.0.4.0')
        self.assertEqual(fake.created_scopes[0]['subnet_mask'], '255.255.252.0')
        self.assertEqual(fake.reservation_calls, [])
        self.assertEqual(fake.created_reservations, [])

    def test_push_never_deletes_server_reservations(self):
        make_unassigned_scope(server=self.server)
        fake = self._sync(
            FakePSUClient(scopes=[dict(FAKE_SCOPE_SNAKE)], reservations={'10.0.1.0': [FAKE_RESERVATION]}),
            push_scope_info=True, push_reservations=True,
        )
        self.assertEqual(fake.deleted_reservations, [])


class ScopeOwnershipGuardTests(TestCase):
    """Which server's sync may act on a scope that only one side has.

    A scope only on the server is deleted from it (Push Scope Info on) or imported into
    NetBox (off). A scope only in NetBox is created on the server (on) or deleted from
    NetBox (off). Only the scope's owner acts: the server for a standalone scope, the
    failover's primary for a failover scope (a secondary standing in may import)."""

    def setUp(self):
        self.primary = make_server(name='Primary', hostname='primary.example.com')
        self.secondary = make_server(name='Secondary', hostname='secondary.example.com')
        self.prefix = make_prefix('10.0.1.0/24')

    def _failover(self, **kwargs):
        return make_failover(name='FO-A', primary=self.primary, secondary=self.secondary, **kwargs)

    def _cases(self):
        # Each arrange() returns (server to sync, failover name the server reports,
        # owner of the NetBox scope, extra sync options).
        def standalone():
            return self.primary, None, {'server': self.primary}, {}

        def failover(sync_from='primary', **kwargs):
            def arrange():
                fo = self._failover(**kwargs)
                return getattr(self, sync_from), 'FO-A', {'failover': fo}, {}
            return arrange

        def standing_in():
            fo = self._failover()
            return self.secondary, 'FO-A', {'failover': fo}, {'fallback_failover_ids': {fo.pk}}

        def standalone_sync_off():
            self.primary.sync_standalone_scopes = False
            self.primary.save()
            self._failover()  # so the pre-flight doesn't skip the server outright
            return self.primary, None, {'server': self.primary}, {}

        def scope_in_maintenance():
            return self.primary, None, {'server': self.primary, 'maintenance_mode': True}, {}

        def other_servers_scope():
            return self.primary, None, {'server': self.secondary}, {}

        def unknown_failover():
            return self.primary, 'FO-Ghost', None, {}

        def two_failovers():
            self._failover()
            make_failover(name='FO-A', primary=self.primary,
                          secondary=make_server(name='Third', hostname='third.example.com'))
            return self.primary, 'FO-A', None, {}

        Y, N, _ = True, False, None  # _ = doesn't apply
        # label: (arrange, (delete from server, import, create on server, delete from NetBox))
        return {
            'standalone scope': (standalone, (Y, Y, Y, Y)),
            'failover, from the primary': (failover(), (Y, Y, Y, Y)),
            'failover in maintenance': (failover(maintenance_mode=True), (N, N, N, N)),
            'failover sync off': (failover(sync_enabled=False), (N, N, N, N)),
            'failover, from the secondary': (failover('secondary'), (N, N, N, N)),
            'failover, secondary standing in': (standing_in, (N, Y, N, N)),
            'standalone sync off': (standalone_sync_off, (N, N, N, N)),
            'scope in maintenance': (scope_in_maintenance, (_, _, N, N)),
            "another server's standalone scope": (other_servers_scope, (_, _, N, N)),
            'unknown failover name': (unknown_failover, (N, N, _, _)),
            'two failovers with the name': (two_failovers, (N, N, _, _)),
        }

    def test_who_acts_on_a_scope_only_one_side_has(self):
        for label, (arrange, (delete, imported, create, removed)) in self._cases().items():
            for push in (True, False):
                with self.subTest(label, push_scope_info=push):
                    server_only, netbox_only = (delete, create) if push else (imported, removed)
                    if server_only is not None:
                        self.assertEqual(self._acts_on_server_only_scope(arrange, push), server_only)
                    if netbox_only is not None:
                        self.assertEqual(self._acts_on_netbox_only_scope(arrange, push), netbox_only)

    @contextmanager
    def _rolled_back(self):
        with transaction.atomic():
            yield
            transaction.set_rollback(True)
        self.primary.refresh_from_db()

    def _acts_on_server_only_scope(self, arrange, push):
        with self._rolled_back():
            server, failover_name, _owner, options = arrange()
            remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='On server only')
            if failover_name:
                remote['failover_name'] = failover_name
            fake = FakePSUClient(scopes=[remote])
            run_sync(server, fake, push_scope_info=push, **options)
            if push:
                return fake.deleted_scopes == ['10.0.1.0']
            return DHCPScope.objects.filter(name='On server only').exists()

    def _acts_on_netbox_only_scope(self, arrange, push):
        with self._rolled_back():
            server, _name, owner, options = arrange()
            scope = make_scope(name='In NetBox only', prefix=self.prefix, **owner)
            fake = FakePSUClient(scopes=[])
            run_sync(server, fake, push_scope_info=push, **options)
            if push:
                return len(fake.created_scopes) == 1
            return not DHCPScope.objects.filter(pk=scope.pk).exists()


class ChangeLoggingTests(TestCase):
    """_change_logging(): changelog attribution plus event-rule flushing via event_tracking()."""

    def test_changes_are_logged_as_service_user(self):
        with _change_logging():
            IPAddress.objects.create(address='10.9.9.1/24', status='dhcp')
        change = ObjectChange.objects.get(changed_object_id=get_ip('10.9.9.1').pk)
        self.assertEqual(change.user.username, 'DHCP-Sync-Service')

    @mock.patch('netbox.context_managers.flush_events')
    def test_events_are_flushed_on_exit(self, flush_events):
        with _change_logging():
            IPAddress.objects.create(address='10.9.9.1/24', status='dhcp')
        flush_events.assert_called_once()
        self.assertEqual(len(flush_events.call_args.args[0]), 1)
        self.assertIsNone(current_request.get())

    @mock.patch('netbox.context_managers.flush_events')
    def test_events_flushed_and_context_cleared_on_error(self, flush_events):
        with self.assertRaises(RuntimeError):
            with _change_logging():
                IPAddress.objects.create(address='10.9.9.1/24', status='dhcp')
                raise RuntimeError('boom')
        # The IP was committed before the error, so its event must still go out.
        flush_events.assert_called_once()
        self.assertIsNone(current_request.get())
