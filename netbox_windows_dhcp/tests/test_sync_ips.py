"""
IP address sync (Sync IP Addresses on): a scope's leases and reservations become NetBox
IPs, with their DNS name, MAC, description, tenant and lease details, and IPs the server
no longer backs are cleaned up. Each test hands _sync_scope_ips the server's lists
directly; no network is used.
"""

import ipaddress
from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase
from django.utils import timezone
from extras.models import Tag
from ipam.models import IPAddress, VRF

from ..background_tasks import INVALID_HOSTNAME_TAG_SLUG, _sync_scope_ips
from ..models import DHCPExclusionRange, DHCPLeaseInfo
from .base import NULL_LOGGER, get_ip, make_prefix, make_scope, make_server, managed_ip
from .fixtures import FAKE_LEASE, FAKE_RESERVATION, lease, reservation

DOCK = '53JL2W3-Dell Pro Thunderbolt 4 Smart Dock.vuhl.root.mrc.local'


def _has_invalid_tag(ip):
    return ip.tags.filter(slug=INVALID_HOSTNAME_TAG_SLUG).exists()


class _ScopeFixture:
    """Scope 10.0.1.0/24, range .10–.254."""

    @classmethod
    def setUpTestData(cls):
        cls.scope = make_scope(prefix=make_prefix('10.0.1.0/24'))

    def _run(self, leases=(), reservations=(), **kwargs):
        return _sync_scope_ips(NULL_LOGGER, self.scope, leases=list(leases), reservations=list(reservations),
                               **kwargs)


class LeaseAndReservationTests(_ScopeFixture, TestCase):

    def test_lease_becomes_a_dhcp_ip(self):
        self._run(leases=[dict(FAKE_LEASE)])
        ip = get_ip('10.0.1.50')
        self.assertEqual((ip.status, ip.dns_name), ('dhcp', 'desktop-abc'))
        self.assertEqual(ip.custom_field_data.get('dhcp_client_id'), '00-11-22-33-44-55')
        info = DHCPLeaseInfo.objects.get(ip_address=ip)
        self.assertEqual(info.lease_hostname, 'desktop-abc')
        self.assertEqual(info.lease_expiration, datetime(2030, 1, 1, tzinfo=dt_timezone.utc))

    def test_reservation_becomes_a_reserved_ip(self):
        self._run(reservations=[dict(FAKE_RESERVATION)])
        ip = get_ip('10.0.1.100')
        self.assertEqual((ip.status, ip.dns_name), ('reserved', 'printer-01'))

    def test_existing_ip_and_lease_details_are_updated(self):
        ip = IPAddress.objects.create(address='10.0.1.50/24', status='dhcp', dns_name='old')
        DHCPLeaseInfo.objects.create(ip_address=ip, lease_hostname='old-host', active=True)
        self._run(leases=[dict(FAKE_LEASE)])
        self.assertEqual(get_ip('10.0.1.50').dns_name, 'desktop-abc')
        self.assertEqual(DHCPLeaseInfo.objects.get(ip_address=ip).lease_hostname, 'desktop-abc')

    def test_push_on_a_lease_never_overwrites_a_reserved_ip(self):
        # NetBox owns the reservation; only a missing MAC is filled in from the lease.
        IPAddress.objects.create(address='10.0.1.100/24', status='reserved', dns_name='printer')
        self._run(leases=[lease('10.0.1.100', hostname=DOCK, client_id='aa-bb')], push_reservations=True)
        ip = get_ip('10.0.1.100')
        self.assertEqual((ip.status, ip.dns_name), ('reserved', 'printer'))
        self.assertEqual(ip.custom_field_data.get('dhcp_client_id'), 'aa-bb')
        self.assertFalse(_has_invalid_tag(ip))

    def test_protected_tag_blocks_writes(self):
        tag = Tag.objects.create(name='Protected', slug='protected')
        ip = IPAddress.objects.create(address='10.0.1.50/24', status='active', dns_name='keep')
        ip.tags.add(tag)
        self._run(leases=[dict(FAKE_LEASE)], protect_tag='protected')
        ip = get_ip('10.0.1.50')
        self.assertEqual((ip.status, ip.dns_name), ('active', 'keep'))

    def test_protected_tag_with_update_client_id_only_updates_the_mac(self):
        tag = Tag.objects.create(name='Protected', slug='protected')
        ip = IPAddress.objects.create(address='10.0.1.50/24', status='dhcp', dns_name='keep')
        ip.tags.add(tag)
        self._run(leases=[lease('10.0.1.50', client_id='new-mac')], protect_tag='protected', update_client_id=True)
        ip = get_ip('10.0.1.50')
        self.assertEqual(ip.dns_name, 'keep')
        self.assertEqual(ip.custom_field_data.get('dhcp_client_id'), 'new-mac')


class InvalidHostnameTests(_ScopeFixture, TestCase):
    """A hostname that isn't a valid DNS name is left out and the IP is tagged instead."""

    def test_invalid_name_is_blanked_and_tagged(self):
        self._run(leases=[lease('10.0.1.50', hostname=DOCK)], reservations=[reservation('10.0.1.100', name=DOCK)])
        for address in ('10.0.1.50', '10.0.1.100'):
            ip = get_ip(address)
            self.assertEqual(ip.dns_name, '', address)
            self.assertTrue(_has_invalid_tag(ip), address)
        # The lease details keep the hostname as the server sent it.
        self.assertEqual(DHCPLeaseInfo.objects.get(ip_address=get_ip('10.0.1.50')).lease_hostname, DOCK)

    def test_existing_invalid_dns_name_is_cleared_and_tagged(self):
        IPAddress.objects.create(address='10.0.1.50/24', status='dhcp', dns_name=DOCK.lower())
        self._run(leases=[lease('10.0.1.50', hostname=DOCK)])
        ip = get_ip('10.0.1.50')
        self.assertEqual(ip.dns_name, '')
        self.assertTrue(_has_invalid_tag(ip))

    def test_valid_or_no_hostname_removes_the_tag(self):
        for hostname, dns_name in (('desktop-abc', 'desktop-abc'), ('', '')):
            with self.subTest(hostname=hostname):
                self._run(leases=[lease('10.0.1.50', hostname=DOCK)])
                IPAddress.objects.filter(pk=get_ip('10.0.1.50').pk).update(dns_name='stale')
                self._run(leases=[lease('10.0.1.50', hostname=hostname)])
                ip = get_ip('10.0.1.50')
                self.assertEqual(ip.dns_name, dns_name)
                self.assertFalse(_has_invalid_tag(ip))

    def test_a_second_run_changes_nothing(self):
        self._run(leases=[lease('10.0.1.50', hostname=DOCK)])
        _, changed = self._run(leases=[lease('10.0.1.50', hostname=DOCK)])
        self.assertEqual(changed, 0)


class DowngradeTests(_ScopeFixture, TestCase):
    """A sync-made reservation the server now only leases becomes a lease IP."""

    def test_downgrade_takes_the_leases_details(self):
        cases = (
            ('desktop-abc', 'desktop-abc', False),
            (DOCK, '', True),
            ('', '', False),
        )
        for hostname, dns_name, tagged in cases:
            with self.subTest(hostname=hostname):
                ip = managed_ip('10.0.1.100', status='reserved', dns_name='printer')
                ip.custom_field_data['dhcp_client_id'] = 'aa-bb'
                ip.save()
                self._run(leases=[lease('10.0.1.100', hostname=hostname, client_id='cc-dd')])
                ip = get_ip('10.0.1.100')
                self.assertEqual((ip.status, ip.dns_name), ('dhcp', dns_name))
                self.assertEqual(ip.custom_field_data.get('dhcp_client_id'), 'cc-dd')
                self.assertEqual(_has_invalid_tag(ip), tagged)
                ip.delete()


class LastUpdatedTests(_ScopeFixture, TestCase):
    """An IP's "last updated" time moves only when the sync really changed it."""

    def _backdate(self, address):
        old = timezone.now() - timedelta(days=30)
        IPAddress.objects.filter(pk=get_ip(address).pk).update(last_updated=old)
        return old

    def test_update_bumps_last_updated(self):
        IPAddress.objects.create(address='10.0.1.50/24', status='dhcp', dns_name='old')
        old = self._backdate('10.0.1.50')
        self._run(leases=[dict(FAKE_LEASE)])
        self.assertGreater(get_ip('10.0.1.50').last_updated, old)

    def test_tag_only_change_bumps_last_updated(self):
        self._run(leases=[lease('10.0.1.50', hostname=DOCK)])
        old = self._backdate('10.0.1.50')
        self._run(leases=[lease('10.0.1.50', hostname='')])  # only the tag is removed
        ip = get_ip('10.0.1.50')
        self.assertFalse(_has_invalid_tag(ip))
        self.assertGreater(ip.last_updated, old)

    def test_reserved_ip_mac_from_a_lease_bumps_last_updated(self):
        IPAddress.objects.create(address='10.0.1.50/24', status='reserved')
        old = self._backdate('10.0.1.50')
        self._run(leases=[dict(FAKE_LEASE)])
        ip = get_ip('10.0.1.50')
        self.assertEqual(ip.custom_field_data.get('dhcp_client_id'), '00-11-22-33-44-55')
        self.assertGreater(ip.last_updated, old)

    def test_unchanged_ip_keeps_last_updated(self):
        self._run(leases=[dict(FAKE_LEASE)])
        old = self._backdate('10.0.1.50')
        self._run(leases=[dict(FAKE_LEASE)])
        self.assertEqual(get_ip('10.0.1.50').last_updated, old)


class LeaseActiveTests(_ScopeFixture, TestCase):
    """Active means the server has a lease on the IP right now."""

    def _info(self, address):
        return DHCPLeaseInfo.objects.get(ip_address__address__net_host=address)

    def test_reservation_without_a_lease_is_inactive(self):
        self._run(reservations=[dict(FAKE_RESERVATION)])
        info = self._info('10.0.1.100')
        self.assertEqual((info.active, info.lease_expiration), (False, None))

    def test_reservation_in_use_is_active(self):
        in_use = lease('10.0.1.100', hostname='client-host', address_state='ActiveReservation')
        self._run(reservations=[dict(FAKE_RESERVATION)], leases=[in_use])
        info = self._info('10.0.1.100')
        self.assertTrue(info.active)
        self.assertIsNotNone(info.lease_expiration)
        self.assertEqual(info.lease_hostname, 'client-host')

    def test_kept_ip_the_server_no_longer_reports_becomes_inactive(self):
        # push_reservations on: NetBox keeps its reserved IP even when the server has nothing.
        ip = IPAddress.objects.create(address='10.0.1.100/24', status='reserved')
        DHCPLeaseInfo.objects.create(ip_address=ip, lease_hostname='old', active=True,
                                     lease_expiration='2026-01-01T00:00:00Z')
        self._run(push_reservations=True)
        info = self._info('10.0.1.100')
        self.assertEqual((info.active, info.lease_expiration, info.lease_hostname), (False, None, 'old'))


class ReservationDescriptionTests(_ScopeFixture, TestCase):
    """With Push Reservations off, the server's reservation description is copied to the IP."""

    def test_description_is_pulled_updated_and_cleared(self):
        self._run(reservations=[reservation('10.0.1.100', description='Front desk printer')])
        self.assertEqual(get_ip('10.0.1.100').description, 'Front desk printer')
        self._run(reservations=[reservation('10.0.1.100', description='')])
        self.assertEqual(get_ip('10.0.1.100').description, '')

    def test_missing_description_leaves_netbox_alone(self):
        IPAddress.objects.create(address='10.0.1.100/24', status='reserved', description='keep')
        res = dict(FAKE_RESERVATION)
        del res['description']
        self._run(reservations=[res])
        self.assertEqual(get_ip('10.0.1.100').description, 'keep')

    def test_push_on_never_pulls_the_description(self):
        IPAddress.objects.create(address='10.0.1.100/24', status='reserved', description='netbox')
        self._run(reservations=[reservation('10.0.1.100', description='server')], push_reservations=True)
        self.assertEqual(get_ip('10.0.1.100').description, 'netbox')

    def test_long_description_is_trimmed(self):
        self._run(reservations=[reservation('10.0.1.100', description='d' * 300)])
        self.assertEqual(get_ip('10.0.1.100').description, 'd' * 200)


IN_RANGE = '10.0.1.20'
IN_EXCLUSION = '10.0.1.55'
OUTSIDE = '10.0.1.200'


class CleanupTests(TestCase):
    """
    Scope 10.0.1.0/24, range .10–.100, exclusion .50–.60. Inside the range the server wins;
    inside the exclusion or outside the range only sync-made IPs are cleaned up.
    """

    @classmethod
    def setUpTestData(cls):
        cls.scope = make_scope(prefix=make_prefix('10.0.1.0/24'), start_ip='10.0.1.10', end_ip='10.0.1.100')
        DHCPExclusionRange.objects.create(scope=cls.scope, start_ip='10.0.1.50', end_ip='10.0.1.60')

    def _run(self, leases=(), reservations=(), **kwargs):
        _sync_scope_ips(NULL_LOGGER, self.scope, leases=list(leases), reservations=list(reservations), **kwargs)

    def _ip(self, address, status, managed=False, client_id=''):
        if managed:
            ip = managed_ip(address, status=status)
        else:
            ip = IPAddress.objects.create(address=f'{address}/24', status=status)
        if client_id:
            ip.custom_field_data['dhcp_client_id'] = client_id
            ip.save()
        return ip

    def _exists(self, ip):
        return IPAddress.objects.filter(pk=ip.pk).exists()

    # --- inside the range: the server wins ---

    def test_in_range_ips_the_server_doesnt_back_are_deleted(self):
        for status in ('active', 'dhcp'):
            for push in (False, True):
                with self.subTest(status=status, push_reservations=push):
                    ip = self._ip(IN_RANGE, status)
                    self._run(push_reservations=push)
                    self.assertFalse(self._exists(ip))

    def test_in_range_device_assigned_ip_is_deleted(self):
        from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
        site = Site.objects.create(name='Site', slug='site')
        manufacturer = Manufacturer.objects.create(name='Maker', slug='maker')
        device_type = DeviceType.objects.create(manufacturer=manufacturer, model='Box', slug='box')
        role = DeviceRole.objects.create(name='Role', slug='role')
        device = Device.objects.create(name='dev', site=site, device_type=device_type, role=role)
        interface = Interface.objects.create(device=device, name='eth0', type='1000base-t')
        ip = IPAddress.objects.create(address=f'{IN_RANGE}/24', status='active', assigned_object=interface)
        self._run()
        self.assertFalse(self._exists(ip))

    def test_in_range_hand_made_reservation(self):
        # Push on: NetBox wins, kept. Push off: not on the server, removed (with or without a MAC).
        for client_id in ('', 'aa-bb-cc-dd-ee-ff'):
            with self.subTest(client_id=client_id):
                ip = self._ip(IN_RANGE, 'reserved', client_id=client_id)
                self._run(push_reservations=True)
                self.assertTrue(self._exists(ip))
                self._run(push_reservations=False)
                self.assertFalse(self._exists(ip))

    def test_in_range_reservation_with_only_a_lease(self):
        ip = self._ip(IN_RANGE, 'reserved')
        self._run(leases=[lease(IN_RANGE)], push_reservations=True)
        ip.refresh_from_db()
        self.assertEqual(ip.status, 'reserved')  # push on: kept as a reservation
        self._run(leases=[lease(IN_RANGE)])
        ip.refresh_from_db()
        self.assertEqual((ip.status, ip.dns_name), ('dhcp', 'desktop-abc'))  # push off: the lease wins

    def test_in_range_server_backed_ips_are_kept(self):
        self._run(leases=[lease('10.0.1.21')], reservations=[reservation(IN_RANGE)])
        self.assertEqual(get_ip(IN_RANGE).status, 'reserved')
        self.assertEqual(get_ip('10.0.1.21').status, 'dhcp')

    def test_in_range_protected_ips_are_kept(self):
        tag = Tag.objects.create(name='Protect', slug='protect')
        tagged = self._ip(IN_RANGE, 'active')
        tagged.tags.add(tag)
        self._run(protect_tag='protect')
        self.assertTrue(self._exists(tagged))
        in_protected_prefix = self._ip('10.0.1.21', 'active')
        self._run(protected_prefix_networks={ipaddress.ip_network('10.0.1.16/28')})
        self.assertTrue(self._exists(in_protected_prefix))

    # --- inside an exclusion, or outside the range ---

    def test_outside_hand_made_ips_are_left_alone(self):
        for where in (IN_EXCLUSION, OUTSIDE):
            for status in ('active', 'reserved', 'dhcp'):
                with self.subTest(where=where, status=status):
                    ip = self._ip(where, status)
                    self._run()
                    self.assertTrue(self._exists(ip))
                    ip.delete()

    def test_outside_sync_made_stale_lease_is_deleted(self):
        for where in (IN_EXCLUSION, OUTSIDE):
            for push in (False, True):
                with self.subTest(where=where, push_reservations=push):
                    ip = self._ip(where, 'dhcp', managed=True)
                    self._run(push_reservations=push)
                    self.assertFalse(self._exists(ip))

    def test_outside_sync_made_stale_reservation(self):
        for where in (IN_EXCLUSION, OUTSIDE):
            with self.subTest(where=where):
                ip = self._ip(where, 'reserved', managed=True, client_id='aa-bb-cc-dd-ee-ff')
                self._run(push_reservations=True)
                self.assertTrue(self._exists(ip))  # push on: never deleted anywhere
                self._run(push_reservations=False)
                self.assertFalse(self._exists(ip))

    def test_outside_sync_made_reservation_with_a_lease_is_downgraded(self):
        ip = self._ip(OUTSIDE, 'reserved', managed=True, client_id='aa-bb-cc-dd-ee-ff')
        self._run(leases=[lease(OUTSIDE)])
        ip.refresh_from_db()
        self.assertEqual(ip.status, 'dhcp')

    def test_outside_sync_made_active_ip_is_left_alone(self):
        ip = self._ip(OUTSIDE, 'active', managed=True)
        self._run()
        self.assertTrue(self._exists(ip))

    def test_server_reservation_inside_an_exclusion_is_kept(self):
        self._run(reservations=[reservation(IN_EXCLUSION)])
        self.assertEqual(get_ip(IN_EXCLUSION).status, 'reserved')
        self._run(reservations=[reservation(IN_EXCLUSION)])  # and survives the next cleanup
        self.assertIsNotNone(get_ip(IN_EXCLUSION))

    def test_ips_outside_the_prefix_are_never_touched(self):
        ip = IPAddress.objects.create(address='10.0.2.20/24', status='active')
        self._run()
        self.assertTrue(self._exists(ip))


class PrefixTenantTests(TestCase):
    """New IPs take the scope prefix's tenant; the sync never re-tenants an existing IP."""

    @classmethod
    def setUpTestData(cls):
        from tenancy.models import Tenant
        cls.tenant = Tenant.objects.create(name='Tenant A', slug='tenant-a')
        cls.other = Tenant.objects.create(name='Tenant B', slug='tenant-b')
        cls.scope = make_scope(prefix=make_prefix('10.0.1.0/24', tenant=cls.tenant))

    def test_new_ips_get_the_prefix_tenant(self):
        _sync_scope_ips(NULL_LOGGER, self.scope, leases=[dict(FAKE_LEASE)], reservations=[dict(FAKE_RESERVATION)])
        self.assertEqual(get_ip('10.0.1.50').tenant, self.tenant)
        self.assertEqual(get_ip('10.0.1.100').tenant, self.tenant)

    def test_no_prefix_tenant_means_no_tenant(self):
        scope = make_scope(name='No Tenant', prefix=make_prefix('10.0.2.0/24'), server=self.scope.server,
                           start_ip='10.0.2.10', end_ip='10.0.2.254')
        _sync_scope_ips(NULL_LOGGER, scope, leases=[lease('10.0.2.50')], reservations=[])
        self.assertIsNone(get_ip('10.0.2.50').tenant)

    def test_existing_ip_tenant_is_not_changed(self):
        IPAddress.objects.create(address='10.0.1.50/24', status='dhcp', tenant=self.other)
        IPAddress.objects.create(address='10.0.1.100/24', status='reserved')
        _sync_scope_ips(NULL_LOGGER, self.scope, leases=[dict(FAKE_LEASE)], reservations=[dict(FAKE_RESERVATION)])
        self.assertEqual(get_ip('10.0.1.50').tenant, self.other)
        self.assertIsNone(get_ip('10.0.1.100').tenant)

    def test_a_tenant_never_keeps_a_stale_ip(self):
        managed_ip('10.0.1.50', tenant=self.other)
        _sync_scope_ips(NULL_LOGGER, self.scope, leases=[], reservations=[])
        self.assertIsNone(get_ip('10.0.1.50'))


class VRFTests(TestCase):
    """A scope only ever sees the IPs in its own prefix's VRF."""

    @classmethod
    def setUpTestData(cls):
        cls.red = VRF.objects.create(name='Red')
        server = make_server()
        make_scope(name='Global', prefix=make_prefix('10.0.1.0/24'), server=server)
        cls.red_scope = make_scope(name='Red', prefix=make_prefix('10.0.1.0/24', vrf=cls.red), server=server)

    def _run(self, leases=(), reservations=()):
        _sync_scope_ips(NULL_LOGGER, self.red_scope, leases=list(leases), reservations=list(reservations))

    def test_new_ip_is_created_in_the_prefix_vrf(self):
        self._run(leases=[dict(FAKE_LEASE)])
        self.assertIsNotNone(get_ip('10.0.1.50', vrf=self.red))
        self.assertIsNone(get_ip('10.0.1.50', vrf=None))

    def test_only_the_scopes_vrf_is_updated(self):
        global_ip = IPAddress.objects.create(address='10.0.1.50/24', status='active', dns_name='global')
        red_ip = IPAddress.objects.create(address='10.0.1.50/24', status='active', vrf=self.red, dns_name='red')
        reserved_global = IPAddress.objects.create(address='10.0.1.100/24', status='active')
        self._run(leases=[dict(FAKE_LEASE)], reservations=[dict(FAKE_RESERVATION)])
        for ip in (global_ip, red_ip, reserved_global):
            ip.refresh_from_db()
        self.assertEqual((global_ip.dns_name, red_ip.dns_name), ('global', 'desktop-abc'))
        self.assertEqual(reserved_global.status, 'active')
        self.assertEqual(get_ip('10.0.1.100', vrf=self.red).status, 'reserved')

    def test_cleanup_only_touches_the_scopes_vrf(self):
        global_ip = managed_ip('10.0.1.60')
        red_ip = managed_ip('10.0.1.60', vrf=self.red)
        self._run()
        self.assertTrue(IPAddress.objects.filter(pk=global_ip.pk).exists())
        self.assertFalse(IPAddress.objects.filter(pk=red_ip.pk).exists())
