"""
Scope settings between NetBox and a server: options, router, description, on/off state,
failover membership and exclusions, pulled or pushed; and deleting scopes from a server.
"""

from unittest import mock

from django.test import TestCase
from ipam.models import IPAddress

from ..api_client import PSUClientError
from ..background_tasks import (
    _deconfigure_and_delete_scope,
    _delete_scopes,
    _pull_exclusions,
    _pull_options,
    _pull_scope_attributes,
    _pull_scope_failover,
    _push_scope,
    _push_scopes,
    _sync_exclusions,
)
from ..models import DHCPExclusionRange, DHCPOptionCodeDefinition, DHCPPluginSettings, DHCPScope
from .base import (
    NULL_LOGGER,
    make_failover,
    make_option_definition,
    make_option_value,
    make_prefix,
    make_scope,
    make_server,
    run_sync,
    set_plugin_settings,
)
from .fixtures import FAKE_RESERVATION, FAKE_SCOPE_SNAKE, FakePSUClient


LEASE_OPT = {'code': 51, 'value': ['86400'], 'vendor_class': ''}
MIN_VERSION = 'netbox_windows_dhcp.constants.PSU_SCOPE_STATE_MIN_VERSION'


# A scope-list record as a newer PSU script sends it with include_router=false.
REMOTE_NO_ROUTER = {k: v for k, v in FAKE_SCOPE_SNAKE.items() if k != 'router'}


def router_opt(ip, vendor_class=''):
    return {'code': 3, 'value': [ip], 'vendor_class': vendor_class}


def _remote(scope_id='10.0.1.0', state='Inactive', **kwargs):
    return dict(FAKE_SCOPE_SNAKE, scope_id=scope_id, state=state, **kwargs)


class _Fixture:

    def setUp(self):
        super().setUp()
        set_plugin_settings(sync_ip_addresses=False, push_reservations=False, push_scope_info=False)
        self.server = make_server(psu_script_version='2.0.0')
        self.prefix = make_prefix('10.0.1.0/24')
        patcher = mock.patch(MIN_VERSION, '2.0.0')
        patcher.start()
        self.addCleanup(patcher.stop)

    def _scope(self, **kwargs):
        # Matches FAKE_SCOPE_SNAKE, so the only possible difference is the state.
        kwargs.setdefault('server', self.server)
        return make_scope(name='Building A', prefix=self.prefix, router='10.0.1.1', **kwargs)

    def _sync(self, fake, server=None, push_scope_info=True, **kwargs):
        run_sync(server or self.server, fake, push_scope_info=push_scope_info, **kwargs)
        return fake


class PullOptionsTests(TestCase):
    """_pull_options — called when push_scope_info=False (DHCP is authoritative)."""

    def setUp(self):
        self.scope = make_scope(prefix=make_prefix('10.0.1.0/24'))

    def test_adds_option_present_on_server_but_missing_in_netbox(self):
        _pull_options(
            NULL_LOGGER, self.scope,
            [{'code': 6, 'name': 'DNS Servers', 'value': ['10.0.0.1', '10.0.0.2']}],
        )

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

        _pull_options(NULL_LOGGER, self.scope, [])

        self.assertFalse(self.scope.option_values.filter(pk=shared_value.pk).exists())
        # The DHCPOptionValue row itself must survive — another scope still uses it.
        self.assertTrue(other_scope.option_values.filter(pk=shared_value.pk).exists())

    def test_changed_value_replaces_link_without_mutating_shared_value(self):
        opt_def = make_option_definition(code=202, name='Test Option 202')
        old_value = make_option_value(option_definition=opt_def, value='old-value')
        self.scope.option_values.add(old_value)

        _pull_options(NULL_LOGGER, self.scope, [{'code': 202, 'value': ['new-value']}])

        current = self.scope.option_values.get(option_definition=opt_def)
        self.assertEqual(current.value, 'new-value')
        old_value.refresh_from_db()
        self.assertEqual(old_value.value, 'old-value')  # untouched, not mutated in place

    def test_matching_option_is_left_alone(self):
        opt_def = make_option_definition(code=203, name='Test Option 203')
        value = make_option_value(option_definition=opt_def, value='unchanged')
        self.scope.option_values.add(value)

        _pull_options(NULL_LOGGER, self.scope, [{'code': 203, 'value': ['unchanged']}])

        self.assertEqual(list(self.scope.option_values.all()), [value])

    def test_router_and_lease_time_codes_are_never_touched(self):
        _pull_options(
            NULL_LOGGER, self.scope,
            [{'code': 3, 'value': ['10.0.1.1']}, {'code': 51, 'value': [43200]}],
        )

        self.assertEqual(self.scope.option_values.count(), 0)


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

    def test_a_router_option_value_is_never_sent_as_an_option(self):
        # The router (code 3) is the scope's own field, sent as 'router'.
        router_def = DHCPOptionCodeDefinition.objects.get(code=3)
        self.scope.option_values.add(make_option_value(option_definition=router_def, value='10.0.1.99'))
        fake = FakePSUClient()

        _push_scope(NULL_LOGGER, fake, self.scope, remote=None, scope_id=None)

        payload = fake.created_scopes[0]
        self.assertEqual(payload['router'], '10.0.1.1')
        self.assertNotIn(3, [o['code'] for o in payload.get('options', {}).get('set', [])])

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


class SyncExclusionsTests(TestCase):
    """Push on: the server's exclusions are made to match NetBox's."""

    def test_missing_created_server_only_deleted_matching_untouched(self):
        scope = make_scope(prefix=make_prefix('10.0.1.0/24'))
        DHCPExclusionRange.objects.create(scope=scope, start_ip='10.0.1.200', end_ip='10.0.1.210')  # on both
        DHCPExclusionRange.objects.create(scope=scope, start_ip='10.0.1.220', end_ip='10.0.1.230')  # NetBox only
        fake = FakePSUClient(exclusions={'10.0.1.0': [
            {'scope_id': '10.0.1.0', 'start_ip': '10.0.1.200', 'end_ip': '10.0.1.210'},
            {'scope_id': '10.0.1.0', 'start_ip': '10.0.1.240', 'end_ip': '10.0.1.250'},  # server only
        ]})

        _sync_exclusions(NULL_LOGGER, fake, scope, '10.0.1.0')

        self.assertEqual([(e['start_ip'], e['end_ip']) for e in fake.created_exclusions],
                         [('10.0.1.220', '10.0.1.230')])
        self.assertEqual([(e['start_ip'], e['end_ip']) for e in fake.deleted_exclusions],
                         [('10.0.1.240', '10.0.1.250')])


class PullRouterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        set_plugin_settings(sync_ip_addresses=False, push_scope_info=False)
        cls.server = make_server()

    def setUp(self):
        self.scope = make_scope(
            name='Building A', prefix=make_prefix('10.0.1.0/24'), server=self.server,
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )

    def _sync(self, fake):
        run_sync(self.server, fake)
        self.scope.refresh_from_db()

    def test_scope_list_is_asked_without_the_router(self):
        fake = FakePSUClient(scopes=[dict(REMOTE_NO_ROUTER)], scope_options={'10.0.1.0': [LEASE_OPT]})
        self._sync(fake)
        self.assertEqual(fake.list_scopes_include_router, [False])

    def test_router_comes_from_the_options(self):
        fake = FakePSUClient(
            scopes=[dict(REMOTE_NO_ROUTER)],
            scope_options={'10.0.1.0': [LEASE_OPT, router_opt('10.0.1.99')]},
        )
        self._sync(fake)
        self.assertEqual(self.scope.router, '10.0.1.99')

    def test_no_option_3_clears_the_router(self):
        fake = FakePSUClient(scopes=[dict(REMOTE_NO_ROUTER)], scope_options={'10.0.1.0': [LEASE_OPT]})
        self._sync(fake)
        self.assertIsNone(self.scope.router)

    def test_failed_options_read_leaves_the_router(self):
        fake = FakePSUClient(
            scopes=[dict(REMOTE_NO_ROUTER)],
            options_error=PSUClientError('simulated timeout', status_code=504),
        )
        self._sync(fake)
        self.assertEqual(self.scope.router, '10.0.1.1')

    def test_scope_missing_from_the_options_leaves_the_router(self):
        fake = FakePSUClient(scopes=[dict(REMOTE_NO_ROUTER)], scope_options={})
        self._sync(fake)
        self.assertEqual(self.scope.router, '10.0.1.1')

    def test_older_script_router_is_still_used(self):
        fake = FakePSUClient(
            scopes=[dict(FAKE_SCOPE_SNAKE, router='10.0.1.77')],
            scope_options={'10.0.1.0': [LEASE_OPT, router_opt('10.0.1.99')]},
        )
        self._sync(fake)
        self.assertEqual(self.scope.router, '10.0.1.77')

    def test_pull_attributes_without_router_key_leaves_it(self):
        _pull_scope_attributes(NULL_LOGGER, self.scope, dict(REMOTE_NO_ROUTER))
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.router, '10.0.1.1')


class PushRouterTests(TestCase):
    def setUp(self):
        self.scope = make_scope(
            name='Building A', prefix=make_prefix('10.0.1.0/24'),
            start_ip='10.0.1.10', end_ip='10.0.1.254', router='10.0.1.1',
        )

    def _push(self, fake):
        return _push_scope(NULL_LOGGER, fake, self.scope, remote=dict(REMOTE_NO_ROUTER), scope_id='10.0.1.0')

    def test_matching_router_in_options_pushes_nothing(self):
        fake = FakePSUClient(scope_options={'10.0.1.0': [LEASE_OPT, router_opt('10.0.1.1')]})
        self.assertFalse(self._push(fake))
        self.assertEqual(fake.updated_scopes, [])

    def test_different_router_in_options_is_pushed(self):
        fake = FakePSUClient(scope_options={'10.0.1.0': [LEASE_OPT, router_opt('10.0.1.99')]})
        self.assertTrue(self._push(fake))
        _sid, payload = fake.updated_scopes[0]
        self.assertEqual(payload['router'], '10.0.1.1')

    def test_router_and_lease_options_are_never_removed(self):
        fake = FakePSUClient(scope_options={'10.0.1.0': [LEASE_OPT, router_opt('10.0.1.99')]})
        self._push(fake)
        _sid, payload = fake.updated_scopes[0]
        self.assertNotIn('options', payload)

    def test_failed_options_read_does_not_push_the_router(self):
        fake = FakePSUClient(scope_options_error=PSUClientError('simulated timeout', status_code=504))
        self.assertFalse(self._push(fake))
        self.assertEqual(fake.updated_scopes, [])


class ScopeDescriptionPullTests(TestCase):

    def setUp(self):
        self.scope = make_scope(
            name='Building A', prefix=make_prefix('10.0.1.0/24'), router='10.0.1.1',
            description='Old',
        )

    def test_pulls_description(self):
        changed = _pull_scope_attributes(NULL_LOGGER, self.scope, dict(FAKE_SCOPE_SNAKE, description='Lobby'))
        self.assertTrue(changed)
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.description, 'Lobby')

    def test_blank_server_description_clears_netbox(self):
        _pull_scope_attributes(NULL_LOGGER, self.scope, dict(FAKE_SCOPE_SNAKE, description=''))
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.description, '')

    def test_missing_description_key_leaves_netbox_alone(self):
        changed = _pull_scope_attributes(NULL_LOGGER, self.scope, dict(FAKE_SCOPE_SNAKE))
        self.assertFalse(changed)
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.description, 'Old')

    def test_pascal_case_key(self):
        _pull_scope_attributes(NULL_LOGGER, self.scope, dict(FAKE_SCOPE_SNAKE, Description='Lobby'))
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.description, 'Lobby')

    def test_long_description_is_trimmed(self):
        _pull_scope_attributes(NULL_LOGGER, self.scope, dict(FAKE_SCOPE_SNAKE, description='x' * 300))
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.description, 'x' * 200)


class ScopeDescriptionPushTests(TestCase):

    def setUp(self):
        self.scope = make_scope(
            name='Building A', prefix=make_prefix('10.0.1.0/24'), router='10.0.1.1',
            description='Lobby',
        )

    def test_create_sends_description(self):
        fake = FakePSUClient()
        _push_scope(NULL_LOGGER, fake, self.scope)
        self.assertEqual(fake.created_scopes[0]['description'], 'Lobby')

    def test_update_sends_description_when_it_differs(self):
        fake = FakePSUClient()
        pushed = _push_scope(
            NULL_LOGGER, fake, self.scope,
            remote=dict(FAKE_SCOPE_SNAKE, description='Something else'), scope_id='10.0.1.0',
        )
        self.assertTrue(pushed)
        self.assertEqual(fake.updated_scopes[0][1]['description'], 'Lobby')

    def test_matching_description_is_not_a_diff(self):
        fake = FakePSUClient()
        pushed = _push_scope(
            NULL_LOGGER, fake, self.scope,
            remote=dict(FAKE_SCOPE_SNAKE, description='Lobby'), scope_id='10.0.1.0',
        )
        self.assertFalse(pushed)
        self.assertEqual(fake.updated_scopes, [])

    def test_other_change_no_longer_wipes_server_description(self):
        fake = FakePSUClient()
        _push_scope(
            NULL_LOGGER, fake, self.scope,
            remote=dict(FAKE_SCOPE_SNAKE, description='Lobby', router='10.0.1.99'), scope_id='10.0.1.0',
        )
        self.assertEqual(fake.updated_scopes[0][1]['description'], 'Lobby')


class ExclusionDescriptionTests(TestCase):

    def test_pull_keeps_description_of_matching_exclusion(self):
        scope = make_scope(prefix=make_prefix('10.0.1.0/24'))
        DHCPExclusionRange.objects.create(
            scope=scope, start_ip='10.0.1.200', end_ip='10.0.1.210', description='Printers',
        )
        _pull_exclusions(NULL_LOGGER, scope, [
            {'start_ip': '10.0.1.200', 'end_ip': '10.0.1.210'},
            {'start_ip': '10.0.1.220', 'end_ip': '10.0.1.230'},
        ])
        kept = DHCPExclusionRange.objects.get(scope=scope, start_ip='10.0.1.200')
        self.assertEqual(kept.description, 'Printers')
        added = DHCPExclusionRange.objects.get(scope=scope, start_ip='10.0.1.220')
        self.assertEqual(added.description, '')


class PullActiveStateTests(_Fixture, TestCase):

    def test_state_is_pulled_with_push_scope_info_off(self):
        scope = self._scope()
        self._sync(FakePSUClient(scopes=[_remote(state='Inactive')]), push_scope_info=False)
        scope.refresh_from_db()
        self.assertFalse(scope.active)
        self._sync(FakePSUClient(scopes=[_remote(state='Active')]), push_scope_info=False)
        scope.refresh_from_db()
        self.assertTrue(scope.active)

    def test_missing_state_counts_as_active(self):
        scope = self._scope(active=False)
        self._sync(FakePSUClient(scopes=[dict(FAKE_SCOPE_SNAKE)]), push_scope_info=False)
        scope.refresh_from_db()
        self.assertTrue(scope.active)

    def test_server_only_inactive_scope_is_imported_as_inactive(self):
        self._sync(FakePSUClient(scopes=[_remote()]), push_scope_info=False)
        self.assertEqual(list(DHCPScope.objects.values_list('network', 'active')), [('10.0.1.0', False)])


class PushActiveStateTests(_Fixture, TestCase):

    def test_netbox_active_switches_an_inactive_server_scope_on(self):
        self._scope()
        fake = self._sync(FakePSUClient(scopes=[_remote(state='Inactive')]))
        self.assertEqual((fake.deleted_scopes, fake.created_scopes), ([], []))
        self.assertEqual([(sid, p['state']) for sid, p in fake.updated_scopes], [('10.0.1.0', 'Active')])

    def test_netbox_inactive_switches_the_server_scope_off(self):
        self._scope(active=False)
        fake = self._sync(FakePSUClient(scopes=[_remote(state='Active')]))
        self.assertEqual([(sid, p['state']) for sid, p in fake.updated_scopes], [('10.0.1.0', 'InActive')])

    def test_matching_state_pushes_nothing(self):
        self._scope(active=False)
        fake = self._sync(FakePSUClient(scopes=[_remote(state='Inactive')]))
        self.assertEqual(fake.updated_scopes, [])

    def test_switching_on_keeps_the_scope_reservations(self):
        self._scope()
        ip = IPAddress.objects.create(address='10.0.1.100/24', status='reserved')
        ip.custom_field_data['dhcp_client_id'] = FAKE_RESERVATION['client_id']
        ip.save()
        fake = FakePSUClient(
            scopes=[_remote(state='Inactive')],
            reservations={'10.0.1.0': [dict(FAKE_RESERVATION)]},
            leases={'10.0.1.0': []},
        )
        self._sync(fake, push_reservations=True)
        self.assertEqual(fake.deleted_scopes, [])
        self.assertEqual(fake.deleted_reservations, [])
        self.assertEqual(len(fake.updated_scopes), 1)

    def test_old_psu_script_is_never_sent_the_state(self):
        self._scope()
        with mock.patch(MIN_VERSION, '9.0.0'):
            fake = self._sync(FakePSUClient(scopes=[_remote(state='Inactive')]))
        # Not a difference an old script could act on, so nothing is pushed every sync.
        self.assertEqual(fake.updated_scopes, [])

    def test_create_sends_the_state(self):
        self._scope(active=False)
        fake = self._sync(FakePSUClient(scopes=[]))
        self.assertEqual([p['state'] for p in fake.created_scopes], ['InActive'])

    def test_create_on_an_old_psu_script_leaves_the_state_out(self):
        self._scope(active=False)
        with mock.patch(MIN_VERSION, None):
            fake = self._sync(FakePSUClient(scopes=[]))
        self.assertEqual(len(fake.created_scopes), 1)
        self.assertNotIn('state', fake.created_scopes[0])

    def test_server_only_inactive_scope_is_deleted(self):
        fake = self._sync(FakePSUClient(scopes=[_remote('10.0.9.0')]))
        self.assertEqual(fake.deleted_scopes, ['10.0.9.0'])
        self.assertFalse(DHCPScope.objects.exists())

    def test_overlapping_inactive_scope_goes_the_normal_server_only_way(self):
        make_scope(prefix=make_prefix('10.0.0.0/23'), server=self.server,
                   start_ip='10.0.0.10', end_ip='10.0.1.250')
        fake = self._sync(FakePSUClient(scopes=[_remote('10.0.1.0')]))
        self.assertEqual(fake.deleted_scopes, ['10.0.1.0'])
        self.assertEqual([s['scope_id'] for s in fake.created_scopes], ['10.0.0.0'])

    def test_failover_scope_is_pushed_to_the_primary_and_replicated(self):
        secondary = make_server(name='Sec', hostname='sec.example.com', psu_script_version='2.0.0')
        self._scope(server=None, failover=make_failover(primary=self.server, secondary=secondary))
        remote = _remote(state='Inactive', failover_name='Failover 1')
        fake = self._sync(FakePSUClient(scopes=[remote]))
        self.assertEqual([p['state'] for _, p in fake.updated_scopes], ['Active'])
        self.assertEqual(fake.replicated_failover_calls, [['10.0.1.0']])
        # The secondary never pushes it: the change reaches it through replication.
        fake = self._sync(FakePSUClient(scopes=[remote]), server=secondary)
        self.assertEqual((fake.updated_scopes, fake.replicated_failover_calls), ([], []))


class PushScopesActiveStateTests(_Fixture, TestCase):

    def _push(self, fake, scope):
        set_plugin_settings(push_scope_info=True)
        _push_scopes(NULL_LOGGER, fake, self.server, [scope.pk], cfg=DHCPPluginSettings.load())
        return fake

    def test_inactive_server_scope_is_updated_in_place(self):
        fake = self._push(FakePSUClient(scopes=[_remote(state='Inactive')]), self._scope())
        self.assertEqual((fake.deleted_scopes, fake.created_scopes), ([], []))
        self.assertEqual([p['state'] for _, p in fake.updated_scopes], ['Active'])

    def test_create_sends_the_state(self):
        fake = self._push(FakePSUClient(scopes=[]), self._scope(active=False))
        self.assertEqual([p['state'] for p in fake.created_scopes], ['InActive'])


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
        failover = make_failover(name='FO-A', primary=self.server)
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


class DeconfigureAndDeleteScopeTests(TestCase):
    """Deleting a scope from a server. A failover scope is taken out of the failover first
    (Windows then removes it from the secondary too); the secondary is never called."""

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

    def test_a_failed_delete_never_raises(self):
        # One scope's failure, of any kind, must not stop the rest of a delete batch.
        for error in (PSUClientError('boom'), ValueError('boom')):
            with self.subTest(error=type(error).__name__):
                class FailingDelete(FakePSUClient):
                    def delete_scope(self, scope_id, error=error):
                        raise error

                _deconfigure_and_delete_scope(NULL_LOGGER, FailingDelete(), '10.0.1.0')

    def test_failed_deconfigure_still_attempts_delete(self):
        class FailingUpdate(FakePSUClient):
            def update_scope(self, scope_id, payload):
                raise ValueError('boom')

        fake = FailingUpdate()

        _deconfigure_and_delete_scope(NULL_LOGGER, fake, '10.0.1.0', failover_name='FO-A')

        self.assertEqual(fake.deleted_scopes, ['10.0.1.0'])


class DeleteScopesTests(TestCase):
    """The delete job's list of scopes (snapshotted when they were deleted from NetBox).
    The scope rows are gone, so the server's and failover's settings are read live."""

    def test_which_deletes_reach_the_server(self):
        standalone = {'scope_id': '10.0.1.0', 'scope_name': 'A'}
        in_failover = dict(standalone, failover_name='FO-A')
        removed_from_failover = [('10.0.1.0', {'failover': {'remove': 'FO-A'}})]
        # label: (server settings, failover sync (None = no failover row), item, expected updates, deleted?)
        cases = {
            'standalone': ({'sync_standalone_scopes': True}, None, standalone, [], True),
            'standalone sync off': ({'sync_standalone_scopes': False}, None, standalone, [], False),
            'scope in maintenance': ({}, None, dict(standalone, maintenance_mode=True), [], False),
            'failover': ({}, True, in_failover, removed_from_failover, True),
            'failover sync off': ({}, False, in_failover, [], False),
            'failover no longer in NetBox': ({}, None, in_failover, removed_from_failover, True),
        }
        for i, (label, (server_kwargs, failover_sync, item, updates, deleted)) in enumerate(cases.items()):
            with self.subTest(label):
                server = make_server(name=f'S{i}', hostname=f's{i}.example.com', **server_kwargs)
                if failover_sync is not None:
                    make_failover(name='FO-A', primary=server, sync_enabled=failover_sync,
                                  secondary=make_server(name=f'S{i}b', hostname=f's{i}b.example.com'))
                fake = FakePSUClient()

                _delete_scopes(NULL_LOGGER, fake, server, [item])

                self.assertEqual(fake.updated_scopes, updates)
                self.assertEqual(fake.deleted_scopes, ['10.0.1.0'] if deleted else [])

    def test_one_failure_does_not_abort_the_batch(self):
        class FlakyClient(FakePSUClient):
            def delete_scope(self, scope_id):
                if scope_id == '10.0.1.0':
                    raise PSUClientError('boom')
                super().delete_scope(scope_id)

        fake = FlakyClient()

        _delete_scopes(NULL_LOGGER, fake, make_server(), [
            {'scope_id': '10.0.1.0', 'scope_name': 'A'},
            {'scope_id': '10.0.2.0', 'scope_name': 'B'},
        ])

        self.assertEqual(fake.deleted_scopes, ['10.0.2.0'])
