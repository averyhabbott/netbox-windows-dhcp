"""
Sync state-machine tests. The sync helpers take leases/reservations/client as
arguments, so most are tested directly with canned data; _sync_server is tested
end-to-end with a FakePSUClient patched in. No network is used.
"""

from unittest import mock

from django.test import TestCase
from extras.models import Tag
from ipam.models import IPAddress

from ..background_tasks import (
    _cleanup_stale_ips,
    _sync_server,
    _update_ip_addresses_from_leases,
    _upsert_ip_address,
)
from ..models import DHCPLeaseInfo
from .base import (
    FAKE_LEASE,
    FAKE_RESERVATION,
    FAKE_SCOPE_SNAKE,
    FakePSUClient,
    NULL_LOGGER,
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
