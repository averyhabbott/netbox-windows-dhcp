"""
Sync state-machine tests. The sync helpers take leases/reservations/client as
arguments, so most are tested directly with canned data; _sync_server is tested
end-to-end with a FakePSUClient patched in. No network is used.
"""

from unittest import mock

from django.test import TestCase
from extras.models import Tag
from ipam.models import IPAddress

from ..api_client import PSUClientError
from ..background_tasks import (
    _check_server_health,
    _cleanup_stale_ips,
    _deconfigure_and_delete_scope,
    _delete_scopes,
    _local_option_map,
    _pull_options,
    _pull_scope_failover,
    _push_scope,
    _push_scopes,
    _remote_option_map,
    _sync_exclusions,
    _sync_server,
    _update_ip_addresses_from_leases,
    _upsert_ip_address,
)
from ..models import DHCPExclusionRange, DHCPLeaseInfo, DHCPOptionCodeDefinition, DHCPServer
from .base import (
    FAKE_LEASE,
    FAKE_RESERVATION,
    FAKE_SCOPE_SNAKE,
    FakePSUClient,
    NULL_LOGGER,
    make_failover,
    make_option_definition,
    make_option_value,
    make_prefix,
    make_scope,
    make_server,
    set_plugin_settings,
)


def get_ip(ip_str):
    return IPAddress.objects.filter(address__net_host=ip_str).first()


class UpsertIPAddressTests(TestCase):
    def test_creates_new_ip(self):
        _upsert_ip_address(
            NULL_LOGGER, ip_str='10.0.1.50', prefix_len=24, status='dhcp',
            dns_name='Host-A', client_id='00-11-22', lease_hostname='host-a',
        )
        obj = get_ip('10.0.1.50')
        self.assertIsNotNone(obj)
        self.assertEqual(obj.status, 'dhcp')
        self.assertEqual(obj.dns_name, 'host-a')  # lowercased
        self.assertEqual(obj.custom_field_data.get('dhcp_client_id'), '00-11-22')
        self.assertTrue(DHCPLeaseInfo.objects.filter(ip_address=obj).exists())

    def test_updates_existing_ip(self):
        IPAddress.objects.create(address='10.0.1.50/24', status='dhcp', dns_name='old')
        _upsert_ip_address(
            NULL_LOGGER, ip_str='10.0.1.50', prefix_len=24, status='dhcp',
            dns_name='new', client_id='',
        )
        self.assertEqual(get_ip('10.0.1.50').dns_name, 'new')

    def test_reservation_takes_precedence_over_lease(self):
        # Existing reserved IP with no client_id; a lease arrives for the same IP.
        IPAddress.objects.create(address='10.0.1.100/24', status='reserved', dns_name='printer')
        _upsert_ip_address(
            NULL_LOGGER, ip_str='10.0.1.100', prefix_len=24, status='dhcp',
            dns_name='lease-host', client_id='aa-bb',
            lease_status='dhcp', reservation_status='reserved',
        )
        obj = get_ip('10.0.1.100')
        self.assertEqual(obj.status, 'reserved')          # unchanged
        self.assertEqual(obj.dns_name, 'printer')          # unchanged
        self.assertEqual(obj.custom_field_data.get('dhcp_client_id'), 'aa-bb')  # backfilled

    def test_protected_tag_blocks_writes(self):
        tag = Tag.objects.create(name='Protected', slug='protected')
        ip = IPAddress.objects.create(address='10.0.1.50/24', status='active', dns_name='keep')
        ip.tags.add(tag)
        _upsert_ip_address(
            NULL_LOGGER, ip_str='10.0.1.50', prefix_len=24, status='dhcp',
            dns_name='changed', client_id='zz', protect_tag='protected',
        )
        obj = get_ip('10.0.1.50')
        self.assertEqual(obj.status, 'active')   # untouched
        self.assertEqual(obj.dns_name, 'keep')   # untouched

    def test_protected_tag_with_update_client_id_only_updates_mac(self):
        tag = Tag.objects.create(name='Protected', slug='protected')
        ip = IPAddress.objects.create(address='10.0.1.50/24', status='dhcp', dns_name='keep')
        ip.tags.add(tag)
        _upsert_ip_address(
            NULL_LOGGER, ip_str='10.0.1.50', prefix_len=24, status='dhcp',
            dns_name='changed', client_id='new-mac', protect_tag='protected',
            update_client_id=True, lease_status='dhcp',
        )
        obj = get_ip('10.0.1.50')
        self.assertEqual(obj.dns_name, 'keep')  # still protected
        self.assertEqual(obj.custom_field_data.get('dhcp_client_id'), 'new-mac')  # updated


class CleanupStaleIPsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.scope = make_scope(prefix=make_prefix('10.0.1.0/24'))

    def test_deletes_stale_lease(self):
        IPAddress.objects.create(address='10.0.1.50/24', status='dhcp')
        _cleanup_stale_ips(
            NULL_LOGGER, self.scope, lease_ips=set(), reservation_ips=set(),
            push_reservations=False,
        )
        self.assertIsNone(get_ip('10.0.1.50'))

    def test_keeps_active_lease(self):
        IPAddress.objects.create(address='10.0.1.50/24', status='dhcp')
        _cleanup_stale_ips(
            NULL_LOGGER, self.scope, lease_ips={'10.0.1.50'}, reservation_ips=set(),
            push_reservations=False,
        )
        self.assertIsNotNone(get_ip('10.0.1.50'))

    def test_reservation_without_client_id_kept(self):
        IPAddress.objects.create(address='10.0.1.100/24', status='reserved')
        _cleanup_stale_ips(
            NULL_LOGGER, self.scope, lease_ips=set(), reservation_ips=set(),
            push_reservations=False,
        )
        self.assertIsNotNone(get_ip('10.0.1.100'))

    def test_push_reservations_never_removes_reservation(self):
        ip = IPAddress.objects.create(address='10.0.1.100/24', status='reserved')
        ip.custom_field_data['dhcp_client_id'] = 'aa-bb'
        ip.save()
        DHCPLeaseInfo.objects.create(ip_address=ip, lease_hostname='x', active=True)
        _cleanup_stale_ips(
            NULL_LOGGER, self.scope, lease_ips=set(), reservation_ips=set(),
            push_reservations=True,
        )
        self.assertIsNotNone(get_ip('10.0.1.100'))

    def test_managed_reservation_downgraded_to_lease(self):
        ip = IPAddress.objects.create(address='10.0.1.100/24', status='reserved')
        ip.custom_field_data['dhcp_client_id'] = 'aa-bb'
        ip.save()
        DHCPLeaseInfo.objects.create(ip_address=ip, lease_hostname='x', active=True)
        _cleanup_stale_ips(
            NULL_LOGGER, self.scope, lease_ips={'10.0.1.100'}, reservation_ips=set(),
            push_reservations=False,
        )
        self.assertEqual(get_ip('10.0.1.100').status, 'dhcp')


class UpdateFromLeasesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.scope = make_scope(prefix=make_prefix('10.0.1.0/24'))

    def test_parses_expiry_and_creates_lease_info(self):
        _update_ip_addresses_from_leases(NULL_LOGGER, self.scope, [dict(FAKE_LEASE)])
        obj = get_ip('10.0.1.50')
        self.assertEqual(obj.status, 'dhcp')
        info = DHCPLeaseInfo.objects.get(ip_address=obj)
        self.assertIsNotNone(info.lease_expiration)


class SyncServerEndToEndTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        set_plugin_settings(sync_ip_addresses=True, push_scope_info=False)
        cls.server = make_server()
        cls.prefix = make_prefix('10.0.1.0/24')
        cls.scope = make_scope(
            name='Building A', prefix=cls.prefix, server=cls.server,
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )

    def test_leases_and_reservations_create_ips(self):
        fake = FakePSUClient(
            scopes=[dict(FAKE_SCOPE_SNAKE)],
            leases={'10.0.1.0': [dict(FAKE_LEASE)]},
            reservations={'10.0.1.0': [dict(FAKE_RESERVATION)]},
            exclusions={'10.0.1.0': []},
        )
        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, self.server,
                sync_ip_addresses=True, push_reservations=False, push_scope_info=False,
            )
        lease_ip = get_ip('10.0.1.50')
        res_ip = get_ip('10.0.1.100')
        self.assertEqual(lease_ip.status, 'dhcp')
        self.assertEqual(res_ip.status, 'reserved')


class OptionMapTests(TestCase):
    """_local_option_map / _remote_option_map both exclude codes 3/51 — Router and
    Lease Time are managed via DHCPScope's own fields, not DHCPOptionValue rows."""

    def test_local_option_map_excludes_router_and_lease_time(self):
        scope = make_scope(prefix=make_prefix('10.0.1.0/24'))
        router_def = DHCPOptionCodeDefinition.objects.get(code=3)
        scope.option_values.add(make_option_value(option_definition=router_def, value='10.0.1.1'))
        scope.option_values.add(make_option_value(value='guest.vu.local'))  # code 200 (test default)

        result = _local_option_map(scope)

        self.assertNotIn(3, result)
        self.assertIn(200, result)

    def test_remote_option_map_excludes_router_and_lease_time(self):
        remote_raw = [
            {'code': 3, 'value': ['10.0.1.1']},
            {'code': 51, 'value': [86400]},
            {'code': 6, 'value': ['10.0.0.1', '10.0.0.2']},
        ]

        result = _remote_option_map(remote_raw)

        self.assertEqual(result, {6: '10.0.0.1, 10.0.0.2'})


class PullOptionsTests(TestCase):
    """_pull_options — called when push_scope_info=False (DHCP is authoritative)."""

    def setUp(self):
        self.scope = make_scope(prefix=make_prefix('10.0.1.0/24'))

    def test_adds_option_present_on_server_but_missing_in_netbox(self):
        # This is the reported bug: an option exists on the DHCP server but not in
        # NetBox, and a pull-direction sync should add it.
        fake = FakePSUClient(scope_options={
            '10.0.1.0': [{'code': 6, 'name': 'DNS Servers', 'value': ['10.0.0.1', '10.0.0.2']}],
        })

        _pull_options(NULL_LOGGER, fake, self.scope, '10.0.1.0')

        values = list(self.scope.option_values.all())
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].option_definition.code, 6)
        self.assertEqual(values[0].value, '10.0.0.1, 10.0.0.2')

    def test_removes_option_no_longer_on_server_but_keeps_shared_value(self):
        opt_def = make_option_definition(code=201, name='Test Option 201')
        shared_value = make_option_value(option_definition=opt_def, value='10.0.0.9')
        other_scope = make_scope(
            name='Other Scope', prefix=make_prefix('10.0.2.0/24'), server=self.scope.server,
        )
        self.scope.option_values.add(shared_value)
        other_scope.option_values.add(shared_value)

        fake = FakePSUClient(scope_options={'10.0.1.0': []})
        _pull_options(NULL_LOGGER, fake, self.scope, '10.0.1.0')

        self.assertFalse(self.scope.option_values.filter(pk=shared_value.pk).exists())
        # The DHCPOptionValue row itself must survive — another scope still uses it.
        self.assertTrue(other_scope.option_values.filter(pk=shared_value.pk).exists())

    def test_changed_value_replaces_link_without_mutating_shared_value(self):
        opt_def = make_option_definition(code=202, name='Test Option 202')
        old_value = make_option_value(option_definition=opt_def, value='old-value')
        self.scope.option_values.add(old_value)

        fake = FakePSUClient(scope_options={
            '10.0.1.0': [{'code': 202, 'value': ['new-value']}],
        })
        _pull_options(NULL_LOGGER, fake, self.scope, '10.0.1.0')

        current = self.scope.option_values.get(option_definition=opt_def)
        self.assertEqual(current.value, 'new-value')
        old_value.refresh_from_db()
        self.assertEqual(old_value.value, 'old-value')  # untouched, not mutated in place

    def test_matching_option_is_left_alone(self):
        opt_def = make_option_definition(code=203, name='Test Option 203')
        value = make_option_value(option_definition=opt_def, value='unchanged')
        self.scope.option_values.add(value)

        fake = FakePSUClient(scope_options={
            '10.0.1.0': [{'code': 203, 'value': ['unchanged']}],
        })
        _pull_options(NULL_LOGGER, fake, self.scope, '10.0.1.0')

        self.assertEqual(list(self.scope.option_values.all()), [value])

    def test_router_and_lease_time_codes_are_never_touched(self):
        fake = FakePSUClient(scope_options={
            '10.0.1.0': [{'code': 3, 'value': ['10.0.1.1']}, {'code': 51, 'value': [43200]}],
        })

        _pull_options(NULL_LOGGER, fake, self.scope, '10.0.1.0')

        self.assertEqual(self.scope.option_values.count(), 0)

    def test_psu_error_leaves_options_unchanged(self):
        from ..api_client import PSUClientError

        opt_def = make_option_definition(code=204)
        value = make_option_value(option_definition=opt_def, value='kept')
        self.scope.option_values.add(value)

        fake = mock.Mock()
        fake.list_scope_options.side_effect = PSUClientError('connection refused')

        _pull_options(NULL_LOGGER, fake, self.scope, '10.0.1.0')

        self.assertEqual(list(self.scope.option_values.all()), [value])


class PushScopeOptionsTests(TestCase):
    """_push_scope — option handling when push_scope_info=True (NetBox is authoritative).
    Options are folded into the same create/update payload as scope attributes,
    rather than a separate endpoint/round-trip."""

    def setUp(self):
        self.prefix = make_prefix('10.0.1.0/24')
        self.scope = make_scope(
            name='Building A', prefix=self.prefix,
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )

    def test_create_includes_all_local_options(self):
        opt_def = make_option_definition(code=205, name='Test Option 205')
        self.scope.option_values.add(make_option_value(option_definition=opt_def, value='a, b'))
        fake = FakePSUClient()

        _push_scope(NULL_LOGGER, fake, self.scope, remote=None, scope_id=None)

        self.assertEqual(len(fake.created_scopes), 1)
        payload = fake.created_scopes[0]
        self.assertEqual(payload['options'], {'set': [{'code': 205, 'value': ['a', 'b']}], 'remove': []})

    def test_update_computes_set_and_remove_against_remote(self):
        keep_def = make_option_definition(code=206, name='Keep')
        self.scope.option_values.add(make_option_value(option_definition=keep_def, value='new-value'))
        fake = FakePSUClient(scope_options={
            '10.0.1.0': [
                {'code': 206, 'value': ['old-value']},  # changed -> must be re-set
                {'code': 207, 'value': ['stale']},       # absent locally -> must be removed
            ],
        })

        _push_scope(NULL_LOGGER, fake, self.scope, remote=dict(FAKE_SCOPE_SNAKE), scope_id='10.0.1.0')

        self.assertEqual(len(fake.updated_scopes), 1)
        _scope_id, payload = fake.updated_scopes[0]
        self.assertEqual(payload['options']['set'], [{'code': 206, 'value': ['new-value']}])
        self.assertEqual(payload['options']['remove'], [207])

    def test_no_diff_anywhere_skips_update(self):
        # Attributes match FAKE_SCOPE_SNAKE exactly and there are no option values
        # on either side — nothing should be pushed.
        fake = FakePSUClient(scope_options={'10.0.1.0': []})

        _push_scope(NULL_LOGGER, fake, self.scope, remote=dict(FAKE_SCOPE_SNAKE), scope_id='10.0.1.0')

        self.assertEqual(fake.updated_scopes, [])

    def test_option_only_change_still_triggers_update(self):
        # Attributes match exactly, but an option differs — the update must still
        # fire (a pure-option diff must not be invisible to the "anything changed?" gate).
        opt_def = make_option_definition(code=208)
        self.scope.option_values.add(make_option_value(option_definition=opt_def, value='new'))
        fake = FakePSUClient(scope_options={'10.0.1.0': []})

        _push_scope(NULL_LOGGER, fake, self.scope, remote=dict(FAKE_SCOPE_SNAKE), scope_id='10.0.1.0')

        self.assertEqual(len(fake.updated_scopes), 1)


class SyncExclusionsReturnValueTests(TestCase):
    """_sync_exclusions reports whether it actually changed anything on the
    server — _sync_server uses this to decide if a failover scope needs
    replication, so a silent False here would silently break that wiring."""

    def setUp(self):
        self.scope = make_scope(prefix=make_prefix('10.0.1.0/24'))

    def test_returns_true_when_exclusion_pushed(self):
        DHCPExclusionRange.objects.create(scope=self.scope, start_ip='10.0.1.200', end_ip='10.0.1.210')
        fake = FakePSUClient(exclusions={'10.0.1.0': []})

        changed = _sync_exclusions(NULL_LOGGER, fake, self.scope, '10.0.1.0')

        self.assertTrue(changed)
        self.assertEqual(len(fake.created_exclusions), 1)

    def test_returns_false_when_nothing_differs(self):
        fake = FakePSUClient(exclusions={'10.0.1.0': []})

        changed = _sync_exclusions(NULL_LOGGER, fake, self.scope, '10.0.1.0')

        self.assertFalse(changed)


class PushScopeFailoverTests(TestCase):
    """_push_scope — failover membership handling when push_scope_info=True
    (NetBox is authoritative). Folded into the same create/update payload as
    scope attributes and options, mirroring how `options` already works."""

    def setUp(self):
        self.prefix = make_prefix('10.0.1.0/24')

    def test_create_enrolls_when_scope_has_failover(self):
        failover = make_failover(name='FO-A')
        scope = make_scope(name='Building A', prefix=self.prefix, failover=failover)
        fake = FakePSUClient()

        pushed = _push_scope(NULL_LOGGER, fake, scope, remote=None, scope_id=None)

        self.assertTrue(pushed)
        self.assertEqual(fake.created_scopes[0]['failover'], {'enroll': 'FO-A'})

    def test_create_omits_failover_key_when_standalone(self):
        scope = make_scope(name='Building A', prefix=self.prefix)
        fake = FakePSUClient()

        _push_scope(NULL_LOGGER, fake, scope, remote=None, scope_id=None)

        self.assertNotIn('failover', fake.created_scopes[0])

    def test_update_enrolls_when_remote_has_none(self):
        failover = make_failover(name='FO-A')
        scope = make_scope(
            name='Building A', prefix=self.prefix, failover=failover,
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )
        fake = FakePSUClient(scope_options={'10.0.1.0': []})
        remote = dict(FAKE_SCOPE_SNAKE)  # no failover_name key -> server reports standalone

        pushed = _push_scope(NULL_LOGGER, fake, scope, remote=remote, scope_id='10.0.1.0')

        self.assertTrue(pushed)
        _scope_id, payload = fake.updated_scopes[0]
        self.assertEqual(payload['failover'], {'enroll': 'FO-A'})

    def test_update_removes_when_netbox_standalone_but_remote_enrolled(self):
        scope = make_scope(
            name='Building A', prefix=self.prefix,
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )
        fake = FakePSUClient(scope_options={'10.0.1.0': []})
        remote = dict(FAKE_SCOPE_SNAKE, failover_name='FO-OLD')

        pushed = _push_scope(NULL_LOGGER, fake, scope, remote=remote, scope_id='10.0.1.0')

        self.assertTrue(pushed)
        _scope_id, payload = fake.updated_scopes[0]
        self.assertEqual(payload['failover'], {'remove': 'FO-OLD'})

    def test_update_reassignment_sends_remove_and_enroll(self):
        new_failover = make_failover(name='FO-NEW')
        scope = make_scope(
            name='Building A', prefix=self.prefix, failover=new_failover,
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )
        fake = FakePSUClient(scope_options={'10.0.1.0': []})
        remote = dict(FAKE_SCOPE_SNAKE, failover_name='FO-OLD')

        pushed = _push_scope(NULL_LOGGER, fake, scope, remote=remote, scope_id='10.0.1.0')

        self.assertTrue(pushed)
        _scope_id, payload = fake.updated_scopes[0]
        self.assertEqual(payload['failover'], {'remove': 'FO-OLD', 'enroll': 'FO-NEW'})

    def test_update_matching_failover_is_a_no_op(self):
        failover = make_failover(name='FO-A')
        scope = make_scope(
            name='Building A', prefix=self.prefix, failover=failover,
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )
        fake = FakePSUClient(scope_options={'10.0.1.0': []})
        remote = dict(FAKE_SCOPE_SNAKE, failover_name='FO-A')

        pushed = _push_scope(NULL_LOGGER, fake, scope, remote=remote, scope_id='10.0.1.0')

        self.assertFalse(pushed)
        self.assertEqual(fake.updated_scopes, [])


class PullScopeFailoverTests(TestCase):
    """_pull_scope_failover — called when push_scope_info=False (DHCP is
    authoritative), mirroring _pull_scope_attributes for the failover field."""

    def setUp(self):
        self.server = make_server()
        self.prefix = make_prefix('10.0.1.0/24')

    def test_assigns_matching_failover_and_clears_server(self):
        failover = make_failover(name='FO-A')
        scope = make_scope(name='Building A', prefix=self.prefix, server=self.server)

        _pull_scope_failover(NULL_LOGGER, scope, {'failover_name': 'FO-A'}, self.server)

        scope.refresh_from_db()
        self.assertEqual(scope.failover_id, failover.pk)
        self.assertIsNone(scope.server_id)

    def test_clears_to_standalone_and_sets_server(self):
        failover = make_failover(name='FO-A')
        scope = make_scope(name='Building A', prefix=self.prefix, failover=failover)

        _pull_scope_failover(NULL_LOGGER, scope, {}, self.server)

        scope.refresh_from_db()
        self.assertIsNone(scope.failover_id)
        self.assertEqual(scope.server_id, self.server.pk)

    def test_unknown_remote_failover_logs_error_and_leaves_scope_unchanged(self):
        scope = make_scope(name='Building A', prefix=self.prefix, server=self.server)

        _pull_scope_failover(NULL_LOGGER, scope, {'failover_name': 'GHOST-RELATIONSHIP'}, self.server)

        scope.refresh_from_db()
        self.assertIsNone(scope.failover_id)
        self.assertEqual(scope.server_id, self.server.pk)

    def test_already_matching_is_a_no_op(self):
        failover = make_failover(name='FO-A')
        scope = make_scope(name='Building A', prefix=self.prefix, failover=failover)

        _pull_scope_failover(NULL_LOGGER, scope, {'failover_name': 'FO-A'}, self.server)

        scope.refresh_from_db()
        self.assertEqual(scope.failover_id, failover.pk)


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


class PushScopesTests(TestCase):
    """_push_scopes — used by DHCPScopePushJob to push exactly the given scope
    pks to a server, without touching any other scope/lease/reservation/
    exclusion on that server (unlike _sync_server's full reconcile)."""

    def test_pushes_only_the_given_scope(self):
        server = make_server()
        prefix1 = make_prefix('10.0.1.0/24')
        prefix2 = make_prefix('10.0.2.0/24')
        scope_a = make_scope(name='A', prefix=prefix1, server=server, router='10.0.1.1')
        make_scope(
            name='B (untouched)', prefix=prefix2, server=server,
            start_ip='10.0.2.10', end_ip='10.0.2.254', router='10.0.2.1',
        )
        remote_a = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='A', router='10.0.1.99')
        # Only scope A's data is seeded — if _push_scopes touched scope B too,
        # get_scope('10.0.2.0') would raise a 404 the test never expects.
        fake = FakePSUClient(scopes=[remote_a], scope_options={'10.0.1.0': []}, exclusions={'10.0.1.0': []})

        _push_scopes(NULL_LOGGER, fake, server, [scope_a.pk])

        self.assertEqual(len(fake.updated_scopes), 1)
        self.assertEqual(fake.updated_scopes[0][0], '10.0.1.0')

    def test_creates_scope_not_yet_on_server(self):
        server = make_server()
        prefix = make_prefix('10.0.1.0/24')
        scope = make_scope(name='New Scope', prefix=prefix, server=server, router='10.0.1.1')
        fake = FakePSUClient(scopes=[])

        _push_scopes(NULL_LOGGER, fake, server, [scope.pk])

        self.assertEqual(len(fake.created_scopes), 1)
        self.assertEqual(fake.created_scopes[0]['scope_id'], '10.0.1.0')

    def test_failover_scope_triggers_replication(self):
        primary = make_server(name='Primary', hostname='primary.example.com')
        secondary = make_server(name='Secondary', hostname='secondary.example.com')
        failover = make_failover(name='FO-A', primary=primary, secondary=secondary)
        prefix = make_prefix('10.0.1.0/24')
        scope = make_scope(name='A', prefix=prefix, failover=failover, router='10.0.1.1')
        remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='A', router='10.0.1.99', failover_name='FO-A')
        fake = FakePSUClient(scopes=[remote], scope_options={'10.0.1.0': []}, exclusions={'10.0.1.0': []})

        _push_scopes(NULL_LOGGER, fake, primary, [scope.pk])

        self.assertEqual(fake.replicated_failover_calls, [['10.0.1.0']])

    def test_failover_scope_never_pushed_directly_to_secondary(self):
        """
        Regression test: a failover scope must never be pushed directly to the
        secondary — only the primary. Windows failover replication (triggered
        by the primary's push) is what propagates the change to the secondary.
        """
        primary = make_server(name='Primary', hostname='primary.example.com')
        secondary = make_server(name='Secondary', hostname='secondary.example.com')
        failover = make_failover(name='FO-A', primary=primary, secondary=secondary)
        prefix = make_prefix('10.0.1.0/24')
        scope = make_scope(name='A', prefix=prefix, failover=failover, router='10.0.1.1')
        fake = FakePSUClient(scopes=[])

        _push_scopes(NULL_LOGGER, fake, secondary, [scope.pk])

        self.assertEqual(fake.created_scopes, [])
        self.assertEqual(fake.updated_scopes, [])
        self.assertEqual(fake.replicated_failover_calls, [])

    def test_scope_belonging_to_a_different_server_is_skipped(self):
        """A scope pk passed in that doesn't actually belong to `server` must be
        skipped rather than pushed — targeting is the caller's job (the signal),
        but this is the last line of defense against a stale/mismatched pk."""
        server = make_server(name='Target', hostname='target.example.com')
        other = make_server(name='Other', hostname='other.example.com')
        prefix = make_prefix('10.0.1.0/24')
        scope = make_scope(name='A', prefix=prefix, server=other, router='10.0.1.1')
        fake = FakePSUClient(scopes=[])

        _push_scopes(NULL_LOGGER, fake, server, [scope.pk])

        self.assertEqual(fake.created_scopes, [])
        self.assertEqual(fake.updated_scopes, [])

    def test_no_push_when_already_matches_including_failover_and_router(self):
        """
        Regression test for the bug found in production: fetching remote state
        one scope at a time via GET /api/dhcp/scopes/:scope_id (rather than the
        bulk list) meant `remote` never had router/failover_name, so a scope
        already correctly enrolled in a failover (with a router set) looked
        like it always needed re-pushing, and Windows rejected the redundant
        Add-DhcpServerv4FailoverScope call. list_scopes() returns the complete
        dict, so a scope matching in every respect must not be pushed at all.
        """
        primary = make_server(name='Primary', hostname='primary.example.com')
        secondary = make_server(name='Secondary', hostname='secondary.example.com')
        failover = make_failover(name='FO-A', primary=primary, secondary=secondary)
        prefix = make_prefix('10.0.1.0/24')
        scope = make_scope(name='A', prefix=prefix, failover=failover, router='10.0.1.1')
        remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='A', router='10.0.1.1', failover_name='FO-A')
        fake = FakePSUClient(scopes=[remote], scope_options={'10.0.1.0': []}, exclusions={'10.0.1.0': []})

        _push_scopes(NULL_LOGGER, fake, primary, [scope.pk])

        self.assertEqual(fake.updated_scopes, [])
        self.assertEqual(fake.replicated_failover_calls, [])


class CheckServerHealthTests(TestCase):
    """_check_server_health — shared by DHCPServerSyncJob and DHCPScopePushJob
    so both keep DHCPServer's health bookkeeping current and both fail loudly
    (raise JobFailed) rather than completing silently when unreachable."""

    def test_success_updates_health_fields(self):
        server = make_server()
        fake = FakePSUClient()  # default health => {'version': '1.0.2'}
        job = mock.Mock()

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _check_server_health(NULL_LOGGER, job, server)

        server.refresh_from_db()
        self.assertEqual(server.health_status, DHCPServer.HEALTH_HEALTHY)
        self.assertEqual(server.health_error, '')
        self.assertEqual(server.psu_script_version, '1.0.2')
        self.assertIsNotNone(server.last_health_check)

    def test_failure_marks_unreachable_and_raises_job_failed(self):
        from core.exceptions import JobFailed

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
        self.assertIn('connection refused', server.health_error)
        self.assertIn(server.name, job.error)


class DeconfigureAndDeleteScopeTests(TestCase):
    """_deconfigure_and_delete_scope — shared by DHCPScopeDeleteJob and
    _sync_server's orphan cleanup. A failover-managed scope must be
    deconfigured (which also removes it from the secondary — Windows does
    this as part of Remove-DhcpServerv4FailoverScope) before being deleted,
    and the secondary is never called directly."""

    def test_standalone_deletes_only(self):
        fake = FakePSUClient()

        _deconfigure_and_delete_scope(NULL_LOGGER, fake, '10.0.1.0')

        self.assertEqual(fake.deleted_scopes, ['10.0.1.0'])
        self.assertEqual(fake.updated_scopes, [])

    def test_failover_deconfigures_before_deleting(self):
        fake = FakePSUClient()

        _deconfigure_and_delete_scope(NULL_LOGGER, fake, '10.0.1.0', failover_name='FO-A')

        self.assertEqual(fake.updated_scopes, [('10.0.1.0', {'failover': {'remove': 'FO-A'}})])
        self.assertEqual(fake.deleted_scopes, ['10.0.1.0'])

    def test_delete_failure_is_swallowed(self):
        class FlakyClient(FakePSUClient):
            def delete_scope(self, scope_id):
                raise PSUClientError('boom')

        fake = FlakyClient()

        _deconfigure_and_delete_scope(NULL_LOGGER, fake, '10.0.1.0')  # must not raise

        self.assertEqual(fake.deleted_scopes, [])

    def test_unexpected_exception_during_delete_is_also_swallowed(self):
        """
        Regression test: matches _push_scope's broad `except Exception` —
        one item's failure (of any kind, not just PSUClientError) must not
        abort the rest of a delete batch.
        """
        class BuggyClient(FakePSUClient):
            def delete_scope(self, scope_id):
                raise ValueError('boom')

        fake = BuggyClient()

        _deconfigure_and_delete_scope(NULL_LOGGER, fake, '10.0.1.0')  # must not raise

        self.assertEqual(fake.deleted_scopes, [])

    def test_unexpected_exception_during_deconfigure_still_attempts_delete(self):
        class BuggyClient(FakePSUClient):
            def update_scope(self, scope_id, payload):
                raise ValueError('boom')

        fake = BuggyClient()

        _deconfigure_and_delete_scope(NULL_LOGGER, fake, '10.0.1.0', failover_name='FO-A')  # must not raise

        self.assertEqual(fake.deleted_scopes, ['10.0.1.0'])


class DeleteScopesTests(TestCase):
    """_delete_scopes — used by DHCPScopeDeleteJob to delete exactly the
    snapshotted scopes taken by signals.py's pre_delete receiver."""

    def test_deletes_standalone_scope(self):
        server = make_server(sync_standalone_scopes=True)
        fake = FakePSUClient()

        _delete_scopes(NULL_LOGGER, fake, server, [{'scope_id': '10.0.1.0', 'scope_name': 'A'}])

        self.assertEqual(fake.deleted_scopes, ['10.0.1.0'])

    def test_deconfigures_failover_before_deleting(self):
        server = make_server()
        make_failover(name='FO-A', primary=server, sync_enabled=True)
        fake = FakePSUClient()

        _delete_scopes(NULL_LOGGER, fake, server, [
            {'scope_id': '10.0.1.0', 'scope_name': 'A', 'failover_name': 'FO-A'},
        ])

        self.assertEqual(fake.updated_scopes, [('10.0.1.0', {'failover': {'remove': 'FO-A'}})])
        self.assertEqual(fake.deleted_scopes, ['10.0.1.0'])

    def test_skips_maintenance_mode_item(self):
        server = make_server()
        fake = FakePSUClient()

        _delete_scopes(NULL_LOGGER, fake, server, [
            {'scope_id': '10.0.1.0', 'scope_name': 'A', 'maintenance_mode': True},
        ])

        self.assertEqual(fake.deleted_scopes, [])
        self.assertEqual(fake.updated_scopes, [])

    def test_skips_standalone_item_when_server_sync_standalone_scopes_disabled(self):
        """
        Regression test: mirrors _push_scopes's live check of
        server.sync_standalone_scopes — a standalone scope's delete must not
        reach the server if standalone sync is off, even though the scope
        itself is already gone from NetBox by the time this job runs.
        """
        server = make_server(sync_standalone_scopes=False)
        fake = FakePSUClient()

        _delete_scopes(NULL_LOGGER, fake, server, [{'scope_id': '10.0.1.0', 'scope_name': 'A'}])

        self.assertEqual(fake.deleted_scopes, [])

    def test_skips_failover_item_when_failover_sync_disabled(self):
        """
        Regression test: mirrors _push_scopes's live check of
        failover.sync_enabled — checked fresh against the live DHCPFailover
        row (which still exists, unlike the deleted DHCPScope) rather than a
        pre_delete-time snapshot.
        """
        server = make_server()
        make_failover(name='FO-A', primary=server, sync_enabled=False)
        fake = FakePSUClient()

        _delete_scopes(NULL_LOGGER, fake, server, [
            {'scope_id': '10.0.1.0', 'scope_name': 'A', 'failover_name': 'FO-A'},
        ])

        self.assertEqual(fake.updated_scopes, [])
        self.assertEqual(fake.deleted_scopes, [])

    def test_proceeds_when_snapshotted_failover_no_longer_exists(self):
        """If the DHCPFailover record itself is gone, there's nothing to gate
        the delete on — proceed rather than silently dropping it."""
        server = make_server()
        fake = FakePSUClient()

        _delete_scopes(NULL_LOGGER, fake, server, [
            {'scope_id': '10.0.1.0', 'scope_name': 'A', 'failover_name': 'FO-GONE'},
        ])

        self.assertEqual(fake.updated_scopes, [('10.0.1.0', {'failover': {'remove': 'FO-GONE'}})])
        self.assertEqual(fake.deleted_scopes, ['10.0.1.0'])

    def test_one_failure_does_not_abort_the_batch(self):
        class FlakyClient(FakePSUClient):
            def delete_scope(self, scope_id):
                if scope_id == '10.0.1.0':
                    raise PSUClientError('boom')
                super().delete_scope(scope_id)

        server = make_server()
        fake = FlakyClient()

        _delete_scopes(NULL_LOGGER, fake, server, [
            {'scope_id': '10.0.1.0', 'scope_name': 'A'},
            {'scope_id': '10.0.2.0', 'scope_name': 'B'},
        ])

        self.assertEqual(fake.deleted_scopes, ['10.0.2.0'])


class SyncServerOrphanCleanupTests(TestCase):
    """_sync_server — when push_scope_info=True, a remote scope with no
    matching local DHCPScope is now cleaned up (deconfigured + deleted)
    instead of just logged and skipped, so a scope deleted in NetBox actually
    disappears from the server even if the immediate pre_delete push missed
    it (or predates this fix)."""

    def test_deletes_standalone_orphan(self):
        server = make_server()
        remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='Orphan')
        fake = FakePSUClient(scopes=[remote])

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, server,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=True,
            )

        self.assertEqual(fake.deleted_scopes, ['10.0.1.0'])

    def test_no_cleanup_when_push_scope_info_false(self):
        server = make_server()
        remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='Orphan')
        fake = FakePSUClient(
            scopes=[remote], scope_options={'10.0.1.0': []}, exclusions={'10.0.1.0': []},
        )

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, server,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=False,
            )

        self.assertEqual(fake.deleted_scopes, [])

    def test_deletes_failover_orphan_when_run_against_primary(self):
        primary = make_server(name='Primary', hostname='primary.example.com')
        secondary = make_server(name='Secondary', hostname='secondary.example.com')
        make_failover(name='FO-A', primary=primary, secondary=secondary)
        remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='Orphan', failover_name='FO-A')
        fake = FakePSUClient(scopes=[remote])

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, primary,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=True,
            )

        self.assertEqual(fake.updated_scopes, [('10.0.1.0', {'failover': {'remove': 'FO-A'}})])
        self.assertEqual(fake.deleted_scopes, ['10.0.1.0'])

    def test_leaves_failover_orphan_alone_when_run_against_secondary(self):
        """
        Regression guard: a failover-managed orphan must only be cleaned up
        from the primary's sync — mirroring _push_scopes never targeting the
        secondary directly. Deconfiguring from the primary already removes
        the scope from the secondary as a side effect.
        """
        primary = make_server(name='Primary', hostname='primary.example.com')
        secondary = make_server(name='Secondary', hostname='secondary.example.com')
        make_failover(name='FO-A', primary=primary, secondary=secondary)
        remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='Orphan', failover_name='FO-A')
        fake = FakePSUClient(scopes=[remote])

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, secondary,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=True,
            )

        self.assertEqual(fake.deleted_scopes, [])
        self.assertEqual(fake.updated_scopes, [])

    def test_leaves_failover_orphan_alone_when_failover_in_maintenance_mode(self):
        """
        Regression test: mirrors the matched-scope loop's failover.maintenance_mode
        check a few lines below — an orphan belonging to a failover relationship
        that's in maintenance mode must not be touched either.
        """
        primary = make_server(name='Primary', hostname='primary.example.com')
        secondary = make_server(name='Secondary', hostname='secondary.example.com')
        make_failover(name='FO-A', primary=primary, secondary=secondary, maintenance_mode=True)
        remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='Orphan', failover_name='FO-A')
        fake = FakePSUClient(scopes=[remote])

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, primary,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=True,
            )

        self.assertEqual(fake.deleted_scopes, [])
        self.assertEqual(fake.updated_scopes, [])

    def test_leaves_failover_orphan_alone_when_failover_sync_disabled(self):
        """Regression test: mirrors the matched-scope loop's failover.sync_enabled check."""
        primary = make_server(name='Primary', hostname='primary.example.com')
        secondary = make_server(name='Secondary', hostname='secondary.example.com')
        make_failover(name='FO-A', primary=primary, secondary=secondary, sync_enabled=False)
        remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='Orphan', failover_name='FO-A')
        fake = FakePSUClient(scopes=[remote])

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, primary,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=True,
            )

        self.assertEqual(fake.deleted_scopes, [])
        self.assertEqual(fake.updated_scopes, [])

    def test_leaves_standalone_orphan_alone_when_server_sync_standalone_scopes_disabled(self):
        """Regression test: mirrors the matched-scope loop's server.sync_standalone_scopes check."""
        server = make_server(sync_standalone_scopes=False)
        # A failover must exist so the pre-flight eligibility check doesn't
        # skip connecting to the server entirely before the orphan is even seen.
        make_failover(name='FO-A', primary=server, sync_enabled=True)
        remote = dict(FAKE_SCOPE_SNAKE, scope_id='10.0.1.0', name='Orphan')
        fake = FakePSUClient(scopes=[remote])

        with mock.patch('netbox_windows_dhcp.api_client.PSUClient', return_value=fake):
            _sync_server(
                NULL_LOGGER, server,
                sync_ip_addresses=False, push_reservations=False, push_scope_info=True,
            )

        self.assertEqual(fake.deleted_scopes, [])
