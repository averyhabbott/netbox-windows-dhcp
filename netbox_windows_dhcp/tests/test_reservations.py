"""
Reservation push: matching the server's reservations to NetBox's, placeholder client IDs,
the push job, and the IP save/delete signals that queue it.
"""

from unittest import mock

from core.exceptions import JobFailed
from core.models import Job, ObjectChange
from django.db import transaction
from django.test import TestCase
from extras.models import Tag
from ipam.models import IPAddress, VRF

from ..api_client import PSUClientError
from ..background_tasks import (
    DHCPReservationPushJob,
    DHCPScopePushJob,
    _change_logging,
    _invalid_hostname_tag,
    _reconcile_reservations,
    _sync_scope_ips,
    _sync_server,
)
from ..models import DHCPLeaseInfo, DHCPServer
from ..utils import plugin_write
from .base import (
    NULL_LOGGER,
    PSU_CLIENT,
    make_failover,
    make_job,
    make_prefix,
    make_scope,
    make_server,
    make_unassigned_scope,
    reserved_ip,
    run_sync,
    set_plugin_settings,
)
from .fixtures import FAKE_RESERVATION, FAKE_SCOPE_SNAKE, FakePSUClient


SCOPE_ID = '10.0.1.0'
BY_IP = 'netbox_windows_dhcp.utils.psu_supports_reservation_by_ip'
ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPReservationPushJob.enqueue'
ENQUEUE_ONCE = 'netbox_windows_dhcp.background_tasks.DHCPSyncJob.enqueue_once'


def server_res(address, client_id='aa-bb-cc-dd-ee-01', name='', description='', type='Both'):
    return {
        'scope_id': SCOPE_ID, 'ip_address': address, 'client_id': client_id,
        'name': name, 'description': description, 'type': type,
    }


def _ip(address, vrf=None):
    return IPAddress.objects.filter(address__net_host=address, vrf=vrf).first()


class ReconcileReservationsTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scope = make_scope(prefix=make_prefix('10.0.1.0/24'))

    def _run(self, reservations=(), fake=None, by_ip=True):
        fake = fake or FakePSUClient()
        result = _reconcile_reservations(
            NULL_LOGGER, fake, self.scope, SCOPE_ID, list(reservations), by_ip=by_ip,
        )
        return fake, result

    # --- creates ---

    def test_missing_reservation_created_with_type_both(self):
        reserved_ip('10.0.1.100', client_id='AA:BB:CC:DD:EE:01', dns_name='printer', description='desk')
        fake, (changed, failed) = self._run()
        self.assertEqual(fake.created_reservations, [{
            'scope_id': SCOPE_ID, 'ip_address': '10.0.1.100', 'client_id': 'aa-bb-cc-dd-ee-01',
            'name': 'printer', 'description': 'desk', 'type': 'Both',
        }])
        self.assertEqual((changed, failed), (True, 0))

    def test_ip_outside_range_is_still_pushed(self):
        reserved_ip('10.0.1.2')  # the scope range starts at .10
        fake, _ = self._run()
        self.assertEqual([r['ip_address'] for r in fake.created_reservations], ['10.0.1.2'])

    def test_ip_without_client_id_is_not_pushed(self):
        reserved_ip('10.0.1.100', client_id='')
        fake, (changed, _) = self._run()
        self.assertEqual(fake.reservation_calls, [])
        self.assertFalse(changed)

    def test_duplicate_client_id_skipped_even_when_spelled_differently(self):
        reserved_ip('10.0.1.100', client_id='aa-bb-cc-dd-ee-01')
        reserved_ip('10.0.1.101', client_id='AABBCCDDEE01')
        fake, _ = self._run()
        self.assertEqual([r['ip_address'] for r in fake.created_reservations], ['10.0.1.100'])

    # --- updates ---

    def test_matching_reservation_is_left_alone(self):
        reserved_ip('10.0.1.100', client_id='AA:BB:CC:DD:EE:01', dns_name='printer', description='d')
        fake, (changed, _) = self._run([server_res('10.0.1.100', name='printer', description='d')])
        self.assertEqual(fake.reservation_calls, [])
        self.assertFalse(changed)

    def test_each_differing_field_is_updated_on_its_own(self):
        reserved_ip('10.0.1.100', dns_name='printer', description='d')
        cases = (
            ({'client_id': 'aa-bb-cc-dd-ee-99'}, {'client_id': 'aa-bb-cc-dd-ee-01'}),
            ({'name': 'old'}, {'name': 'printer'}),
            ({'description': 'old'}, {'description': 'd'}),
            ({'type': 'Dhcp'}, {'type': 'Both'}),
        )
        for remote, sent in cases:
            with self.subTest(remote):
                base = server_res('10.0.1.100', name='printer', description='d')
                fake, (changed, _) = self._run([dict(base, **remote)])
                self.assertEqual(fake.updated_reservations,
                                 [{'scope_id': SCOPE_ID, 'ip_address': '10.0.1.100', **sent}])
                self.assertTrue(changed)
                self.assertEqual(fake.created_reservations, [])

    def test_name_compared_ignoring_case(self):
        reserved_ip('10.0.1.100', dns_name='printer')
        fake, _ = self._run([server_res('10.0.1.100', name='PRINTER')])
        self.assertEqual(fake.reservation_calls, [])

    def test_blank_name_clears_the_server_name(self):
        reserved_ip('10.0.1.100', dns_name='')
        fake, _ = self._run([server_res('10.0.1.100', name='old')])
        self.assertEqual(fake.updated_reservations[0]['name'], '')

    def test_invalid_hostname_tag_with_blank_name_leaves_server_name_alone(self):
        ip = reserved_ip('10.0.1.100', dns_name='')
        ip.tags.add(_invalid_hostname_tag())
        fake, _ = self._run([server_res('10.0.1.100', name='Bob\'s PC')])
        self.assertEqual(fake.reservation_calls, [])

    def test_missing_description_or_type_on_the_server_is_not_compared(self):
        reserved_ip('10.0.1.100', description='d')
        remote = server_res('10.0.1.100')
        del remote['description'], remote['type']
        fake, _ = self._run([remote])
        self.assertEqual(fake.reservation_calls, [])

    # --- deletes ---

    def test_server_only_reservation_deleted(self):
        fake, (changed, _) = self._run([server_res('10.0.1.100')])
        self.assertEqual(fake.deleted_reservations, [{'scope_id': SCOPE_ID, 'ip_address': '10.0.1.100'}])
        self.assertTrue(changed)

    def test_reservation_deleted_when_netbox_ip_is_not_reservation_status(self):
        reserved_ip('10.0.1.100', status='active')
        fake, _ = self._run([server_res('10.0.1.100')])
        self.assertEqual([r['ip_address'] for r in fake.deleted_reservations], ['10.0.1.100'])

    def test_reserved_ip_without_client_id_leaves_server_reservation_alone(self):
        reserved_ip('10.0.1.100', client_id='')
        fake, _ = self._run([server_res('10.0.1.100')])
        self.assertEqual(fake.reservation_calls, [])

    def test_server_values_never_written_to_netbox(self):
        ip = reserved_ip('10.0.1.100', dns_name='printer', description='d')
        self._run([server_res('10.0.1.100', client_id='11-11-11-11-11-11', name='x', description='y')])
        ip.refresh_from_db()
        self.assertEqual((ip.dns_name, ip.description, ip.custom_field_data['dhcp_client_id']),
                         ('printer', 'd', 'aa-bb-cc-dd-ee-01'))

    # --- order ---

    def test_deletes_then_updates_then_creates(self):
        reserved_ip('10.0.1.101', client_id='aa-bb-cc-dd-ee-02')
        reserved_ip('10.0.1.102', client_id='aa-bb-cc-dd-ee-03')
        fake, _ = self._run([
            server_res('10.0.1.100', client_id='aa-bb-cc-dd-ee-01'),
            server_res('10.0.1.101', client_id='aa-bb-cc-dd-ee-99'),
        ])
        self.assertEqual([m for m, _ in fake.reservation_calls], ['DELETE', 'PUT', 'POST'])

    def test_mac_moved_between_ips_frees_it_first(self):
        # .100 takes .101's MAC; .101 gets a new one. .101 must be updated first.
        reserved_ip('10.0.1.100', client_id='aa-bb-cc-dd-ee-02')
        reserved_ip('10.0.1.101', client_id='aa-bb-cc-dd-ee-05')
        fake, _ = self._run([
            server_res('10.0.1.100', client_id='aa-bb-cc-dd-ee-01'),
            server_res('10.0.1.101', client_id='aa-bb-cc-dd-ee-02'),
        ])
        self.assertEqual(fake.reservation_calls, [('PUT', ['10.0.1.101', '10.0.1.100'])])

    # --- failures ---

    def test_partial_failure_counts_and_carries_on(self):
        reserved_ip('10.0.1.100', client_id='aa-bb-cc-dd-ee-01')
        reserved_ip('10.0.1.101', client_id='aa-bb-cc-dd-ee-02')
        fake = FakePSUClient(reservation_results={
            '10.0.1.100': {'status': 'error', 'error': 'boom'},
        })
        with self.assertLogs(NULL_LOGGER, level='WARNING') as cm:
            fake, (changed, failed) = self._run(fake=fake)
        self.assertEqual((changed, failed), (True, 1))
        self.assertEqual([r['ip_address'] for r in fake.created_reservations], ['10.0.1.101'])
        self.assertTrue(any('10.0.1.100' in line and 'boom' in line for line in cm.output))

    def test_refused_swap_is_counted_as_failed(self):
        reserved_ip('10.0.1.100', client_id='aa-bb-cc-dd-ee-02')
        reserved_ip('10.0.1.101', client_id='aa-bb-cc-dd-ee-01')
        error = 'client_id aa-bb-cc-dd-ee-02 is already used by 10.0.1.101 in scope 10.0.1.0.'
        fake = FakePSUClient(reservation_results={
            '10.0.1.100': {'status': 'error', 'error': error},
            '10.0.1.101': {'status': 'error', 'error': error},
        })
        with self.assertLogs(NULL_LOGGER, level='WARNING') as cm:
            _, (changed, failed) = self._run([
                server_res('10.0.1.100', client_id='aa-bb-cc-dd-ee-01'),
                server_res('10.0.1.101', client_id='aa-bb-cc-dd-ee-02'),
            ], fake=fake)
        self.assertEqual((changed, failed), (False, 2))
        self.assertTrue(any('10.0.1.100' in line for line in cm.output))

    def test_update_not_found_is_a_failure_but_delete_not_found_is_not(self):
        reserved_ip('10.0.1.100', dns_name='new')
        fake = FakePSUClient(reservation_results={
            '10.0.1.100': {'status': 'not_found'},
            '10.0.1.200': {'status': 'not_found'},
        })
        _, (_, failed) = self._run([server_res('10.0.1.100', name='old'), server_res('10.0.1.200')],
                                   fake=fake)
        self.assertEqual(failed, 1)

    # --- old PSU script ---

    def test_old_script_creates_one_at_a_time_and_never_updates_or_deletes(self):
        reserved_ip('10.0.1.101', client_id='aa-bb-cc-dd-ee-02')
        reserved_ip('10.0.1.102', client_id='aa-bb-cc-dd-ee-03', dns_name='new')
        fake, (changed, _) = self._run([
            server_res('10.0.1.100'), server_res('10.0.1.102', client_id='aa-bb-cc-dd-ee-03'),
        ], by_ip=False)
        self.assertEqual(fake.reservation_calls, [])  # no bulk calls
        self.assertEqual([r['ip_address'] for r in fake.created_reservations], ['10.0.1.101'])
        self.assertTrue(changed)

    def test_old_script_failed_create_is_counted(self):
        from ..api_client import PSUClientError

        reserved_ip('10.0.1.101')
        fake = FakePSUClient()
        fake.create_reservation = mock.Mock(side_effect=PSUClientError('boom', status_code=500))
        _, (changed, failed) = self._run(fake=fake, by_ip=False)
        self.assertEqual((changed, failed), (False, 1))


class PlaceholderReservationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scope = make_scope(prefix=make_prefix('10.0.1.0/24'), start_ip='10.0.1.10', end_ip='10.0.1.200')

    def _run(self, reservations=(), placeholders=True, **kwargs):
        fake = FakePSUClient()
        with _change_logging():
            _reconcile_reservations(NULL_LOGGER, fake, self.scope, SCOPE_ID, list(reservations),
                                    placeholders=placeholders, **kwargs)
        return fake

    def _client_id(self, ip):
        ip.refresh_from_db()
        return ip.custom_field_data.get('dhcp_client_id')

    def test_placeholder_generated_saved_and_pushed(self):
        ip = reserved_ip('10.0.1.100', client_id='')
        fake = self._run()
        client_id = self._client_id(ip)
        self.assertRegex(client_id, r'^ba-dc-0d-ed(-[0-9a-f]{2}){4}$')
        self.assertEqual(fake.created_reservations[0]['client_id'], client_id)
        change = ObjectChange.objects.filter(changed_object_id=ip.pk).latest('time')
        self.assertEqual(change.user.username, 'DHCP-Sync-Service')
        self.assertEqual(change.postchange_data['custom_fields']['dhcp_client_id'], client_id)

    def test_placeholders_are_unique_in_the_scope(self):
        ips = [reserved_ip(f'10.0.1.{n}', client_id='') for n in range(100, 110)]
        self._run()
        ids = {self._client_id(ip) for ip in ips}
        self.assertEqual(len(ids), 10)

    def test_placeholder_avoids_ids_already_on_the_server_or_in_netbox(self):
        ip = reserved_ip('10.0.1.100', client_id='')
        taken_server = 'ba-dc-0d-ed-00-00-00-01'
        taken_netbox = 'ba-dc-0d-ed-00-00-00-02'
        reserved_ip('10.0.1.101', client_id=taken_netbox)
        with mock.patch('secrets.token_hex', side_effect=['00000001', '00000002', '00000003']):
            self._run([server_res('10.0.1.150', client_id=taken_server)])
        self.assertEqual(self._client_id(ip), 'ba-dc-0d-ed-00-00-00-03')

    def test_not_generated_when_a_condition_fails(self):
        protect = Tag.objects.create(name='Protect', slug='protect')
        cases = (
            ('setting off', '10.0.1.100', 'reserved', {'placeholders': False}),
            ('outside range', '10.0.1.2', 'reserved', {}),
            ('not reserved', '10.0.1.101', 'active', {}),
            ('protected tag', '10.0.1.102', 'reserved', {'protect_tag': 'protect'}),
        )
        for label, address, status, kwargs in cases:
            with self.subTest(label):
                ip = reserved_ip(address, client_id='', status=status)
                ip.tags.add(protect)
                self._run(**kwargs)
                self.assertFalse(self._client_id(ip))
                ip.delete()

    def test_not_generated_in_protected_prefix(self):
        import ipaddress
        ip = reserved_ip('10.0.1.100', client_id='')
        self._run(protected_prefix_networks={ipaddress.ip_network('10.0.1.0/25')})
        self.assertFalse(self._client_id(ip))

    def test_real_mac_entered_later_updates_the_server(self):
        ip = reserved_ip('10.0.1.100', client_id='')
        self._run()
        placeholder = self._client_id(ip)
        ip.custom_field_data['dhcp_client_id'] = 'aa-bb-cc-dd-ee-01'
        ip.save()
        fake = self._run([server_res('10.0.1.100', client_id=placeholder)])
        self.assertEqual(fake.updated_reservations[0]['client_id'], 'aa-bb-cc-dd-ee-01')

    def test_turning_the_setting_off_keeps_existing_placeholders(self):
        ip = reserved_ip('10.0.1.100', client_id='')
        self._run()
        placeholder = self._client_id(ip)
        fake = self._run([server_res('10.0.1.100', client_id=placeholder)], placeholders=False)
        self.assertEqual(self._client_id(ip), placeholder)
        self.assertEqual(fake.reservation_calls, [])


class SyncServerReservationPushTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        set_plugin_settings(sync_ip_addresses=True, push_scope_info=False)

    def _sync(self, server, fake, push=True):
        with mock.patch(PSU_CLIENT, return_value=fake), mock.patch(BY_IP, return_value=True):
            return _sync_server(NULL_LOGGER, server, sync_ip_addresses=True,
                                push_reservations=push, push_scope_info=False)

    def _fake(self, reservations):
        return FakePSUClient(
            scopes=[dict(FAKE_SCOPE_SNAKE)], leases={SCOPE_ID: []},
            reservations={SCOPE_ID: reservations}, exclusions={SCOPE_ID: []},
            scope_options={SCOPE_ID: []},
        )

    def test_server_reservation_never_creates_or_overwrites_netbox_ips(self):
        server = make_server()
        make_scope(name='Building A', prefix=make_prefix('10.0.1.0/24'), server=server, router='10.0.1.1')
        ip = reserved_ip('10.0.1.100', dns_name='printer')
        fake = self._fake([server_res('10.0.1.100', name='server-name'), server_res('10.0.1.150')])
        self._sync(server, fake)
        ip.refresh_from_db()
        self.assertEqual(ip.dns_name, 'printer')
        self.assertFalse(IPAddress.objects.filter(address__net_host='10.0.1.150').exists())
        self.assertTrue(DHCPLeaseInfo.objects.filter(ip_address=ip).exists())
        self.assertEqual([r['ip_address'] for r in fake.deleted_reservations], ['10.0.1.150'])
        self.assertEqual([r['name'] for r in fake.updated_reservations], ['printer'])

    def test_failover_scope_with_reservation_change_is_replicated(self):
        primary = make_server(name='P', hostname='p.example.com')
        failover = make_failover(primary=primary, secondary=make_server(name='S', hostname='s.example.com'))
        make_scope(name='Building A', prefix=make_prefix('10.0.1.0/24'), failover=failover,
                   router='10.0.1.1')
        reserved_ip('10.0.1.100')
        fake = self._fake([])
        fake._scopes = [dict(FAKE_SCOPE_SNAKE, failover_name=failover.name)]
        self._sync(primary, fake)
        self.assertEqual(fake.replicated_failover_calls, [[SCOPE_ID]])

    def test_no_replication_when_reservations_already_match(self):
        primary = make_server(name='P', hostname='p.example.com')
        failover = make_failover(primary=primary, secondary=make_server(name='S', hostname='s.example.com'))
        make_scope(name='Building A', prefix=make_prefix('10.0.1.0/24'), failover=failover,
                   router='10.0.1.1')
        reserved_ip('10.0.1.100')
        fake = self._fake([server_res('10.0.1.100')])
        fake._scopes = [dict(FAKE_SCOPE_SNAKE, failover_name=failover.name)]
        self._sync(primary, fake)
        self.assertEqual(fake.replicated_failover_calls, [])

    def test_failed_changes_are_counted_in_the_result(self):
        server = make_server()
        make_scope(name='Building A', prefix=make_prefix('10.0.1.0/24'), server=server, router='10.0.1.1')
        reserved_ip('10.0.1.100')
        fake = self._fake([])
        fake._reservation_results = {'10.0.1.100': {'status': 'error', 'error': 'boom'}}
        result = self._sync(server, fake)
        self.assertEqual(result['reservation_failures'], 1)

    def test_push_off_still_pulls_reservations(self):
        server = make_server()
        make_scope(name='Building A', prefix=make_prefix('10.0.1.0/24'), server=server, router='10.0.1.1')
        fake = self._fake([server_res('10.0.1.150', name='pulled')])
        self._sync(server, fake, push=False)
        self.assertEqual(IPAddress.objects.get(address__net_host='10.0.1.150').dns_name, 'pulled')
        self.assertEqual(fake.reservation_calls, [])


class ReservationVRFTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.red = VRF.objects.create(name='Red')
        server = make_server()
        cls.global_scope = make_scope(name='Global', prefix=make_prefix('10.0.1.0/24'), server=server)
        cls.red_scope = make_scope(name='Red', prefix=make_prefix('10.0.1.0/24', vrf=cls.red), server=server)

    def test_reservation_push_only_reads_the_scopes_vrf(self):
        for vrf, mac in ((None, 'aa-aa-aa-aa-aa-aa'), (self.red, 'bb-bb-bb-bb-bb-bb')):
            ip = IPAddress.objects.create(address='10.0.1.150/24', status='reserved', vrf=vrf)
            ip.custom_field_data['dhcp_client_id'] = mac
            ip.save()
        fake = FakePSUClient()
        _reconcile_reservations(NULL_LOGGER, fake, self.red_scope, '10.0.1.0', [])
        self.assertEqual([r['client_id'] for r in fake.created_reservations], ['bb-bb-bb-bb-bb-bb'])

    def test_reservation_pass_only_touches_the_scopes_vrf(self):
        global_ip = IPAddress.objects.create(address='10.0.1.100/24', status='active')
        _sync_scope_ips(NULL_LOGGER, self.red_scope, leases=[], reservations=[dict(FAKE_RESERVATION)])
        global_ip.refresh_from_db()
        self.assertEqual(global_ip.status, 'active')
        self.assertEqual(_ip('10.0.1.100', self.red).status, 'reserved')


class ReservationsWithoutPrefixTests(TestCase):

    def setUp(self):
        set_plugin_settings(sync_ip_addresses=True, push_reservations=False, push_scope_info=False)
        self.server = make_server()

    def _sync(self, fake, **kwargs):
        run_sync(self.server, fake, **{'sync_ip_addresses': True, **kwargs})
        return fake

    def test_reconcile_reservations_skips_the_scope(self):
        scope = make_unassigned_scope(server=self.server)
        fake = FakePSUClient()
        result = _reconcile_reservations(NULL_LOGGER, fake, scope, '10.0.1.0', [FAKE_RESERVATION])
        self.assertEqual(result, (False, 0))
        self.assertEqual(fake.reservation_calls, [])


class ReservationSignalTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        set_plugin_settings(push_reservations=True)
        cls.server = make_server()
        cls.scope = make_scope(name='A', prefix=make_prefix('10.0.1.0/24'), server=cls.server)

    def _capture(self, fn):
        with mock.patch(ENQUEUE_ONCE), mock.patch(ENQUEUE) as enq:
            with self.captureOnCommitCallbacks(execute=True):
                fn()
        return enq

    def test_saving_a_reserved_ip_enqueues_its_scope_once(self):
        enq = self._capture(lambda: reserved_ip('10.0.1.100/24'))
        enq.assert_called_once_with(server_pk=self.server.pk, scope_pks=[self.scope.pk])

    def test_resaving_a_reserved_ip_enqueues_its_scope(self):
        with plugin_write():  # setup — mustn't queue a push of its own
            ip = reserved_ip('10.0.1.101/24')
        ip.dns_name = 'renamed'
        self._capture(ip.save).assert_called_once()

    def test_rolled_back_save_is_never_pushed(self):
        other = make_scope(name='B', prefix=make_prefix('10.0.2.0/24'), server=self.server,
                           start_ip='10.0.2.10', end_ip='10.0.2.254')

        def saves():
            try:
                with transaction.atomic():
                    reserved_ip('10.0.2.100/24')
                    raise RuntimeError('rolled back')
            except RuntimeError:
                pass
            reserved_ip('10.0.1.100/24')

        enq = self._capture(saves)
        enq.assert_called_once_with(server_pk=self.server.pk, scope_pks=[self.scope.pk])
        self.assertNotIn(other.pk, enq.call_args.kwargs['scope_pks'])

    def test_no_enqueue_when_push_reservations_off(self):
        set_plugin_settings(push_reservations=False)
        enq = self._capture(lambda: reserved_ip('10.0.1.100/24'))
        self.assertFalse(enq.called)

    def test_non_reserved_ip_is_ignored(self):
        enq = self._capture(lambda: reserved_ip('10.0.1.100/24', status='active'))
        self.assertFalse(enq.called)

    def test_ip_outside_every_scope_is_ignored(self):
        enq = self._capture(lambda: reserved_ip('10.9.9.9/24'))
        self.assertFalse(enq.called)

    def test_status_changed_away_from_reserved_enqueues(self):
        with plugin_write():  # setup — mustn't queue a push of its own
            ip = reserved_ip('10.0.1.100/24')

        def change():
            ip.status = 'active'
            ip.save()
        enq = self._capture(change)
        enq.assert_called_once_with(server_pk=self.server.pk, scope_pks=[self.scope.pk])

    def test_deleting_a_reserved_ip_enqueues(self):
        with plugin_write():  # setup — mustn't queue a push of its own
            ip = reserved_ip('10.0.1.100/24')
        enq = self._capture(ip.delete)
        enq.assert_called_once_with(server_pk=self.server.pk, scope_pks=[self.scope.pk])

    def test_ip_moved_between_scopes_enqueues_both(self):
        other = make_scope(name='B', prefix=make_prefix('10.0.2.0/24'), server=self.server,
                           start_ip='10.0.2.10', end_ip='10.0.2.254')
        with plugin_write():  # setup — mustn't queue a push of its own
            ip = reserved_ip('10.0.1.100/24')

        def move():
            ip.address = '10.0.2.100/24'
            ip.save()
        enq = self._capture(move)
        enq.assert_called_once_with(server_pk=self.server.pk, scope_pks=sorted([self.scope.pk, other.pk]))

    def test_bulk_edit_makes_one_job_per_server(self):
        other_server = make_server(name='S2', hostname='s2.example.com')
        other = make_scope(name='B', prefix=make_prefix('10.0.2.0/24'), server=other_server,
                           start_ip='10.0.2.10', end_ip='10.0.2.254')

        def bulk():
            with transaction.atomic():
                reserved_ip('10.0.1.100/24')
                reserved_ip('10.0.1.101/24', client_id='aa-bb-cc-dd-ee-02')
                reserved_ip('10.0.2.100/24')
        enq = self._capture(bulk)
        calls = sorted((c.kwargs['server_pk'], c.kwargs['scope_pks']) for c in enq.call_args_list)
        self.assertEqual(calls, sorted([(self.server.pk, [self.scope.pk]), (other_server.pk, [other.pk])]))

    def test_failover_scope_targets_the_primary(self):
        primary = make_server(name='P', hostname='p.example.com')
        failover = make_failover(primary=primary, secondary=make_server(name='S', hostname='s.example.com'))
        make_scope(name='B', prefix=make_prefix('10.0.2.0/24'), failover=failover,
                   start_ip='10.0.2.10', end_ip='10.0.2.254')
        enq = self._capture(lambda: reserved_ip('10.0.2.100/24'))
        self.assertEqual(enq.call_args.kwargs['server_pk'], primary.pk)

    def test_plugin_job_writes_never_enqueue(self):
        def sync_write():
            with plugin_write():
                ip = reserved_ip('10.0.1.100/24')
                ip.delete()
        enq = self._capture(sync_write)
        self.assertFalse(enq.called)


class ReservationPushJobTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        set_plugin_settings(push_reservations=True)

    def _run(self, server, scopes, fake, job_class=DHCPReservationPushJob):
        with mock.patch(PSU_CLIENT, return_value=fake), mock.patch(BY_IP, return_value=True):
            job_class(make_job(job_class.name)).run(server_pk=server.pk, scope_pks=[s.pk for s in scopes])

    def _standalone(self):
        server = make_server()
        scope = make_scope(name='A', prefix=make_prefix('10.0.1.0/24'), server=server)
        return server, scope

    def test_pushes_the_scopes_reservations(self):
        server, scope = self._standalone()
        reserved_ip('10.0.1.100/24')
        fake = FakePSUClient(reservations={'10.0.1.0': [
            {'scope_id': '10.0.1.0', 'ip_address': '10.0.1.150', 'client_id': 'ff-ff-ff-ff-ff-ff'},
        ]})
        self._run(server, [scope], fake)
        self.assertEqual([r['ip_address'] for r in fake.created_reservations], ['10.0.1.100'])
        self.assertEqual([r['ip_address'] for r in fake.deleted_reservations], ['10.0.1.150'])
        self.assertEqual(fake.list_reservations_calls, ['10.0.1.0'])  # one scope, one small call
        self.assertEqual(fake.replicated_failover_calls, [])

    def test_several_scopes_share_one_bulk_fetch(self):
        server, scope = self._standalone()
        other = make_scope(name='B', prefix=make_prefix('10.0.2.0/24'), server=server,
                           start_ip='10.0.2.10', end_ip='10.0.2.254')
        fake = FakePSUClient(reservations={'10.0.1.0': [], '10.0.2.0': []})
        self._run(server, [scope, other], fake)
        self.assertEqual(fake.list_reservations_calls, [None])

    def test_failover_scope_is_replicated_once(self):
        primary = make_server(name='P', hostname='p.example.com')
        failover = make_failover(primary=primary, secondary=make_server(name='S', hostname='s.example.com'))
        scope = make_scope(name='A', prefix=make_prefix('10.0.1.0/24'), failover=failover)
        reserved_ip('10.0.1.100/24')
        fake = FakePSUClient(reservations={'10.0.1.0': []})
        self._run(primary, [scope], fake)
        self.assertEqual(fake.replicated_failover_calls, [['10.0.1.0']])

    def test_scope_in_maintenance_is_skipped(self):
        server, scope = self._standalone()
        scope.maintenance_mode = True
        scope.save()
        reserved_ip('10.0.1.100/24')
        fake = FakePSUClient(reservations={'10.0.1.0': []})
        self._run(server, [scope], fake)
        self.assertEqual(fake.reservation_calls, [])
        self.assertEqual(fake.list_reservations_calls, [])

    def test_read_only_server_is_skipped(self):
        server, scope = self._standalone()
        reserved_ip('10.0.1.100/24')
        fake = FakePSUClient(reservations={'10.0.1.0': []},
                             ping_write_error=PSUClientError('forbidden', status_code=403))
        self._run(server, [scope], fake)
        self.assertEqual(fake.created_reservations, [])
        self.assertEqual(DHCPServer.objects.get(pk=server.pk).access_level, DHCPServer.ACCESS_RO)

    def test_push_reservations_off_does_nothing(self):
        set_plugin_settings(push_reservations=False)
        server, scope = self._standalone()
        reserved_ip('10.0.1.100/24')
        fake = FakePSUClient(reservations={'10.0.1.0': []})
        self._run(server, [scope], fake)
        self.assertEqual(fake.created_reservations, [])

    def test_fetch_failure_deletes_nothing(self):
        # Also what happens when the scope isn't on the server yet: the fetch errors.
        server, scope = self._standalone()
        reserved_ip('10.0.1.100/24')
        fake = FakePSUClient(reservations_error=PSUClientError('Scope not found', status_code=500))
        self._run(server, [scope], fake)
        self.assertEqual(fake.reservation_calls, [])

    def test_scope_missing_from_bulk_fetch_is_skipped(self):
        server, scope = self._standalone()
        other = make_scope(name='B', prefix=make_prefix('10.0.2.0/24'), server=server,
                           start_ip='10.0.2.10', end_ip='10.0.2.254')
        reserved_ip('10.0.2.100/24')
        fake = FakePSUClient(reservations={'10.0.1.0': []})  # 10.0.2.0 isn't on the server
        self._run(server, [scope, other], fake)
        self.assertEqual(fake.reservation_calls, [])

    def test_failed_change_turns_the_job_failed(self):
        server, scope = self._standalone()
        reserved_ip('10.0.1.100/24')
        fake = FakePSUClient(reservations={'10.0.1.0': []},
                             reservation_results={'10.0.1.100': {'status': 'error', 'error': 'boom'}})
        with self.assertRaises(JobFailed):
            self._run(server, [scope], fake)
        job = Job.objects.get(name=DHCPReservationPushJob.name)
        self.assertEqual(job.error, f'{server.name}: 1 reservation change(s) failed — see log')


class ScopePushBringsReservationsTests(TestCase):
    """Both push settings on: a pushed scope's reservations follow in the same job/run."""

    @classmethod
    def setUpTestData(cls):
        set_plugin_settings(push_reservations=True, push_scope_info=True)

    def test_scope_push_job_creates_scope_then_its_reservations(self):
        primary = make_server(name='P', hostname='p.example.com')
        failover = make_failover(primary=primary, secondary=make_server(name='S', hostname='s.example.com'))
        with mock.patch('netbox_windows_dhcp.signals._queue_scope_push'):
            scope = make_scope(name='A', prefix=make_prefix('10.0.1.0/24'), failover=failover,
                               router='10.0.1.1')
        with plugin_write():
            reserved_ip('10.0.1.100/24')
        fake = FakePSUClient(scopes=[])
        with mock.patch(PSU_CLIENT, return_value=fake), mock.patch(BY_IP, return_value=True):
            DHCPScopePushJob(make_job(DHCPScopePushJob.name)).run(server_pk=primary.pk, scope_pks=[scope.pk])
        self.assertEqual(len(fake.created_scopes), 1)
        self.assertEqual([r['ip_address'] for r in fake.created_reservations], ['10.0.1.100'])
        self.assertEqual(fake.list_reservations_calls, [])  # a new scope has none to fetch
        self.assertEqual(fake.replicated_failover_calls, [['10.0.1.0']])

    def test_scope_push_job_reconciles_an_existing_scopes_reservations(self):
        server = make_server()
        with mock.patch('netbox_windows_dhcp.signals._queue_scope_push'):
            scope = make_scope(name='Building A', prefix=make_prefix('10.0.1.0/24'), server=server,
                               router='10.0.1.1')
        fake = FakePSUClient(
            scopes=[dict(FAKE_SCOPE_SNAKE)], exclusions={'10.0.1.0': []},
            reservations={'10.0.1.0': [{'scope_id': '10.0.1.0', 'ip_address': '10.0.1.150',
                                        'client_id': 'ff-ff-ff-ff-ff-ff'}]},
        )
        with mock.patch(PSU_CLIENT, return_value=fake), mock.patch(BY_IP, return_value=True):
            DHCPScopePushJob(make_job(DHCPScopePushJob.name)).run(server_pk=server.pk, scope_pks=[scope.pk])
        self.assertEqual([r['ip_address'] for r in fake.deleted_reservations], ['10.0.1.150'])

    def test_scope_push_job_without_push_reservations_leaves_reservations_alone(self):
        set_plugin_settings(push_reservations=False)
        server = make_server()
        with mock.patch('netbox_windows_dhcp.signals._queue_scope_push'):
            scope = make_scope(name='A', prefix=make_prefix('10.0.1.0/24'), server=server, router='10.0.1.1')
        reserved_ip('10.0.1.100/24')
        fake = FakePSUClient(scopes=[])
        with mock.patch(PSU_CLIENT, return_value=fake), mock.patch(BY_IP, return_value=True):
            DHCPScopePushJob(make_job(DHCPScopePushJob.name)).run(server_pk=server.pk, scope_pks=[scope.pk])
        self.assertEqual(fake.created_reservations, [])

    def test_scheduled_sync_pushes_reservations_of_a_scope_it_just_created(self):
        server = make_server()
        with mock.patch('netbox_windows_dhcp.signals._queue_scope_push'):
            make_scope(name='A', prefix=make_prefix('10.0.1.0/24'), server=server, router='10.0.1.1')
        with plugin_write():
            reserved_ip('10.0.1.100/24')
        fake = FakePSUClient(scopes=[], reservations={})
        with mock.patch(PSU_CLIENT, return_value=fake), mock.patch(BY_IP, return_value=True):
            _sync_server(NULL_LOGGER, server, sync_ip_addresses=False,
                         push_reservations=True, push_scope_info=True)
        self.assertEqual(len(fake.created_scopes), 1)
        self.assertEqual([r['ip_address'] for r in fake.created_reservations], ['10.0.1.100'])
