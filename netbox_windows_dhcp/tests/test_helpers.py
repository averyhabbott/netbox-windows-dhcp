"""
The small rules the rest of the plugin is built on: how MAC addresses, lease times,
hostnames and routers are read and written, which scope an IP belongs to, which failover
a name means, and which PSU script versions get which features.
"""

from unittest import mock

from django.test import SimpleTestCase, TestCase
from ipam.models import VRF

from ..background_tasks import _change_logging, _clean_dns_name
from ..constants import PSU_SCRIPT_VERSION
from ..import_logic import remote_has_router, router_from_options
from ..models import DHCPExclusionRange
from ..utils import (
    AmbiguousFailover,
    decompose_lease_lifetime,
    find_failover,
    format_client_id,
    in_exclusion,
    in_scope_range,
    lease_lifetime_display,
    normalize_client_id,
    plugin_write,
    plugin_write_active,
    psu_supports_reservation_by_ip,
    psu_supports_scope_state,
    scope_for_ip,
)
from .base import make_failover, make_prefix, make_scope, make_server
from .fixtures import FAKE_SCOPE_SNAKE


class ClientIdTests(SimpleTestCase):

    def test_normalize(self):
        for raw in ('AA:BB:CC:DD:EE:FF', 'aa-bb-cc-dd-ee-ff', 'aabb.ccdd.eeff', 'AABBCCDDEEFF', ' aa bb cc dd ee ff '):
            self.assertEqual(normalize_client_id(raw), 'aabbccddeeff', raw)
        self.assertEqual(normalize_client_id(None), '')
        self.assertEqual(normalize_client_id(''), '')

    def test_format_windows_style(self):
        cases = {
            'AA:BB:CC:DD:EE:FF': 'aa-bb-cc-dd-ee-ff',
            'badc0ded01234567': 'ba-dc-0d-ed-01-23-45-67',
            '01-aa-bb-cc-dd-ee-ff': '01-aa-bb-cc-dd-ee-ff',
            # Not hex, or an odd length: left normalized.
            'abc': 'abc',
            'zz-zz': 'zzzz',
            '': '',
        }
        for raw, expected in cases.items():
            self.assertEqual(format_client_id(raw), expected, raw)


class LeaseLifetimeTests(SimpleTestCase):
    """Shown in the largest whole unit; the form splits it into a value and a unit the same way."""

    CASES = (
        (259200, '3 Days', (3, 'days')),
        (86400, '1 Day', (1, 'days')),
        (262800, '73 Hours', (73, 'hours')),
        (3600, '1 Hour', (1, 'hours')),
        (1800, '30 Minutes', (30, 'minutes')),
        (60, '1 Minute', (1, 'minutes')),
        (90, '90 Seconds', (90, 'seconds')),
        (1, '1 Second', (1, 'seconds')),
        (0, '0 Seconds', (0, 'seconds')),
    )

    def test_display_and_split(self):
        for seconds, shown, split in self.CASES:
            with self.subTest(seconds=seconds):
                self.assertEqual(lease_lifetime_display(seconds), shown)
                self.assertEqual(decompose_lease_lifetime(seconds), split)


class DnsNameTests(SimpleTestCase):
    """What the sync accepts as an IP's DNS name; anything else is blanked and tagged."""

    def test_clean_dns_name(self):
        dock = '53JL2W3-Dell Pro Thunderbolt 4 Smart Dock.vuhl.root.mrc.local'
        cases = {
            '': ('', False),
            'Desktop-ABC.corp.local': ('desktop-abc.corp.local', False),
            'host_01': ('host_01', False),
            dock: ('', True),
            'bad\\032name': ('', True),
            "kelsey's-phone": ('', True),
            'a..b': ('', True),
            'a' * 256: ('', True),
        }
        for raw, expected in cases.items():
            self.assertEqual(_clean_dns_name(raw), expected, raw)


class RouterFromOptionsTests(SimpleTestCase):

    def test_router_is_option_3(self):
        lease_time = {'code': 51, 'value': ['86400'], 'vendor_class': ''}
        cases = (
            ([lease_time, {'code': 3, 'value': ['10.0.1.1'], 'vendor_class': ''}], '10.0.1.1'),
            ([{'code': 3, 'value': ['10.0.1.1', '10.0.1.2']}], '10.0.1.1'),  # several: the first
            ([lease_time], None),
            ([], None),
            ([{'code': 3, 'value': ['0.0.0.0']}], None),
            ([{'code': 3, 'value': ['10.9.9.9'], 'vendor_class': 'Acme'}], None),  # a vendor class's
        )
        for options, expected in cases:
            self.assertEqual(router_from_options(options), expected, options)

    def test_remote_has_router(self):
        self.assertTrue(remote_has_router(dict(FAKE_SCOPE_SNAKE)))
        self.assertTrue(remote_has_router({'Router': None}))
        self.assertTrue(remote_has_router({'router': None}))
        self.assertFalse(remote_has_router({k: v for k, v in FAKE_SCOPE_SNAKE.items() if k != 'router'}))


class ScopeRangeTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scope = make_scope(prefix=make_prefix('10.0.1.0/24'), start_ip='10.0.1.10', end_ip='10.0.1.100')
        DHCPExclusionRange.objects.create(scope=cls.scope, start_ip='10.0.1.50', end_ip='10.0.1.60')

    def test_in_scope_range(self):
        for ip in ('10.0.1.10', '10.0.1.100', '10.0.1.55/24'):
            self.assertTrue(in_scope_range(self.scope, ip), ip)
        for ip in ('10.0.1.9', '10.0.1.101', 'not-an-ip'):
            self.assertFalse(in_scope_range(self.scope, ip), ip)

    def test_in_exclusion(self):
        for ip in ('10.0.1.50', '10.0.1.60'):
            self.assertTrue(in_exclusion(self.scope, ip), ip)
        for ip in ('10.0.1.49', '10.0.1.61', 'not-an-ip'):
            self.assertFalse(in_exclusion(self.scope, ip), ip)


class ScopeForIpTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.vrf = VRF.objects.create(name='Red')
        cls.server = make_server()
        cls.global_scope = make_scope(name='Global', prefix=make_prefix('10.0.1.0/24'), server=cls.server)
        cls.vrf_scope = make_scope(name='Red', prefix=make_prefix('10.0.1.0/24', vrf=cls.vrf), server=cls.server)
        cls.wide_scope = make_scope(
            name='Wide', prefix=make_prefix('10.0.0.0/16'), server=cls.server,
            start_ip='10.0.0.10', end_ip='10.0.255.200',
        )

    def test_matches_by_vrf_and_most_specific_prefix(self):
        self.assertEqual(scope_for_ip('10.0.1.50', None), self.global_scope)
        self.assertEqual(scope_for_ip('10.0.1.50', self.vrf.pk), self.vrf_scope)
        self.assertEqual(scope_for_ip('10.0.2.50', None), self.wide_scope)

    def test_no_match(self):
        self.assertIsNone(scope_for_ip('192.168.1.1', None))
        self.assertIsNone(scope_for_ip('10.0.2.50', self.vrf.pk))
        self.assertIsNone(scope_for_ip('garbage', None))

    def test_same_prefix_prefers_scope_whose_range_contains_ip(self):
        prefix = make_prefix('10.0.9.0/24')
        make_scope(name='Low', prefix=prefix, server=self.server, start_ip='10.0.9.10', end_ip='10.0.9.50')
        high = make_scope(name='High', prefix=prefix, server=self.server, start_ip='10.0.9.100', end_ip='10.0.9.200')
        self.assertEqual(scope_for_ip('10.0.9.150', None), high)


class FindFailoverTests(TestCase):
    """Failover names are unique per server pair, not overall."""

    def test_same_name_on_two_server_pairs(self):
        dev1, dev2 = make_server(name='Dev 1', hostname='d1'), make_server(name='Dev 2', hostname='d2')
        prod1, prod2 = make_server(name='Prod 1', hostname='p1'), make_server(name='Prod 2', hostname='p2')
        dev = make_failover(name='FO', primary=dev1, secondary=dev2)
        prod = make_failover(name='FO', primary=prod1, secondary=prod2)
        self.assertEqual(find_failover('FO', dev1), dev)
        self.assertEqual(find_failover('FO', dev2), dev)
        self.assertEqual(find_failover('FO', prod2), prod)
        self.assertIsNone(find_failover('FO', make_server(name='Other', hostname='o')))

    def test_two_matches_for_one_server_is_ambiguous(self):
        a, b, c = (make_server(name=n, hostname=n) for n in ('a', 'b', 'c'))
        make_failover(name='FO', primary=a, secondary=b)
        make_failover(name='FO', primary=a, secondary=c)  # leftover data
        with self.assertRaises(AmbiguousFailover):
            find_failover('FO', a)
        self.assertIsNotNone(find_failover('FO', b))


class PsuVersionGateTests(SimpleTestCase):

    def _server(self, version):
        return mock.Mock(psu_script_version=version)

    def test_no_minimum_treats_every_server_as_old(self):
        with mock.patch('netbox_windows_dhcp.constants.PSU_RESERVATION_BY_IP_MIN_VERSION', None):
            self.assertFalse(psu_supports_reservation_by_ip(self._server('99.0.0')))

    def test_compares_versions_numerically(self):
        with mock.patch('netbox_windows_dhcp.constants.PSU_RESERVATION_BY_IP_MIN_VERSION', '1.2.0'):
            for version in ('1.2.0', '1.10.0'):
                self.assertTrue(psu_supports_reservation_by_ip(self._server(version)), version)
            for version in ('1.1.9', '', 'unknown'):
                self.assertFalse(psu_supports_reservation_by_ip(self._server(version)), version)

    def test_shipped_script_passes_its_own_gates(self):
        server = self._server(PSU_SCRIPT_VERSION)
        self.assertTrue(psu_supports_reservation_by_ip(server))
        self.assertTrue(psu_supports_scope_state(server))


class PluginWriteFlagTests(TestCase):
    """The plugin's own writes skip the locks and the push signals; the flag must never stick."""

    def test_set_inside_and_cleared_after(self):
        self.assertFalse(plugin_write_active())
        for context in (plugin_write, _change_logging):
            with self.subTest(context.__name__):
                with context():
                    self.assertTrue(plugin_write_active())
                self.assertFalse(plugin_write_active())

    def test_cleared_after_an_error(self):
        with self.assertRaises(RuntimeError):
            with _change_logging():
                raise RuntimeError('boom')
        self.assertFalse(plugin_write_active())
