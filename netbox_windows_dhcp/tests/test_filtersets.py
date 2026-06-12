"""
FilterSet tests.

NOTE: we intentionally do NOT inherit NetBox's ChangeLoggedFilterSetTests. Its
``test_filters_defined`` asserts that *every* model field has a corresponding
filter; these filtersets deliberately expose only a curated subset (name,
hostname, search, etc.), so the harness would report dozens of fields as
"missing" by design. Instead we test the filters that exist — the custom
``search`` (q) behavior and representative field filters — directly.
"""

from django.test import TestCase

from ..filtersets import (
    DHCPExclusionRangeFilterSet,
    DHCPFailoverFilterSet,
    DHCPOptionCodeDefinitionFilterSet,
    DHCPOptionValueFilterSet,
    DHCPScopeFilterSet,
    DHCPServerFilterSet,
)
from ..models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)
from .base import clear_builtin_option_codes, make_failover, make_prefix, make_scope, make_server


class DHCPServerFilterSetTests(TestCase):
    queryset = DHCPServer.objects.all()
    filterset = DHCPServerFilterSet

    @classmethod
    def setUpTestData(cls):
        make_server(name='Alpha DHCP', hostname='alpha.example.com', port=443)
        make_server(name='Bravo DHCP', hostname='bravo.example.com', port=8443)
        make_server(name='Charlie', hostname='charlie.example.com', port=443)

    def test_q_matches_name(self):
        self.assertEqual(self.filterset({'q': 'Alpha'}, self.queryset).qs.count(), 1)

    def test_q_matches_hostname(self):
        self.assertEqual(self.filterset({'q': 'bravo.example'}, self.queryset).qs.count(), 1)

    def test_name_icontains(self):
        self.assertEqual(self.filterset({'name': 'dhcp'}, self.queryset).qs.count(), 2)

    def test_port(self):
        self.assertEqual(self.filterset({'port': [443]}, self.queryset).qs.count(), 2)


class DHCPFailoverFilterSetTests(TestCase):
    queryset = DHCPFailover.objects.all()
    filterset = DHCPFailoverFilterSet

    @classmethod
    def setUpTestData(cls):
        cls.p = make_server(name='P', hostname='p.example.com')
        cls.s = make_server(name='S', hostname='s.example.com')
        make_failover(name='FO Load', primary=cls.p, secondary=cls.s, mode='LoadBalance')
        make_failover(
            name='FO Hot',
            primary=make_server(name='P2', hostname='p2.example.com'),
            secondary=make_server(name='S2', hostname='s2.example.com'),
            mode='HotStandby',
        )

    def test_q_matches_name(self):
        self.assertEqual(self.filterset({'q': 'Load'}, self.queryset).qs.count(), 1)

    def test_mode(self):
        self.assertEqual(self.filterset({'mode': ['HotStandby']}, self.queryset).qs.count(), 1)

    def test_primary_server_id(self):
        self.assertEqual(
            self.filterset({'primary_server_id': [self.p.pk]}, self.queryset).qs.count(), 1
        )


class DHCPOptionCodeDefinitionFilterSetTests(TestCase):
    queryset = DHCPOptionCodeDefinition.objects.all()
    filterset = DHCPOptionCodeDefinitionFilterSet

    @classmethod
    def setUpTestData(cls):
        clear_builtin_option_codes()  # start from an empty table for deterministic counts
        DHCPOptionCodeDefinition.objects.create(code=200, name='ZZ-TFTP Server')
        DHCPOptionCodeDefinition.objects.create(code=201, name='ZZ-Bootfile', is_builtin=True)
        DHCPOptionCodeDefinition.objects.create(code=202, name='ZZ-Cisco', vendor_class='Cisco')

    def test_q_matches_name(self):
        self.assertEqual(self.filterset({'q': 'ZZ-TFTP'}, self.queryset).qs.count(), 1)

    def test_q_matches_code_int(self):
        self.assertEqual(self.filterset({'q': '200'}, self.queryset).qs.count(), 1)

    def test_is_builtin(self):
        self.assertEqual(self.filterset({'is_builtin': True}, self.queryset).qs.count(), 1)


class DHCPOptionValueFilterSetTests(TestCase):
    queryset = DHCPOptionValue.objects.all()
    filterset = DHCPOptionValueFilterSet

    @classmethod
    def setUpTestData(cls):
        opt = DHCPOptionCodeDefinition.objects.create(code=200, name='DNS')
        DHCPOptionValue.objects.create(option_definition=opt, value='10.0.0.1', friendly_name='Primary DNS')
        DHCPOptionValue.objects.create(option_definition=opt, value='10.0.0.2', friendly_name='Secondary DNS')
        DHCPOptionValue.objects.create(option_definition=opt, value='8.8.8.8', friendly_name='Public')

    def test_q_matches_friendly_name(self):
        self.assertEqual(self.filterset({'q': 'Primary'}, self.queryset).qs.count(), 1)

    def test_q_matches_value(self):
        self.assertEqual(self.filterset({'q': '8.8.8.8'}, self.queryset).qs.count(), 1)

    def test_value_icontains(self):
        self.assertEqual(self.filterset({'value': '10.0.0'}, self.queryset).qs.count(), 2)


class DHCPExclusionRangeFilterSetTests(TestCase):
    queryset = DHCPExclusionRange.objects.all()
    filterset = DHCPExclusionRangeFilterSet

    @classmethod
    def setUpTestData(cls):
        cls.scope = make_scope()
        DHCPExclusionRange.objects.create(scope=cls.scope, start_ip='10.0.1.50', end_ip='10.0.1.60')
        DHCPExclusionRange.objects.create(scope=cls.scope, start_ip='10.0.1.70', end_ip='10.0.1.80')

    def test_scope_id(self):
        self.assertEqual(
            self.filterset({'scope_id': [self.scope.pk]}, self.queryset).qs.count(), 2
        )

    def test_q_matches_start_ip(self):
        self.assertEqual(self.filterset({'q': '10.0.1.50'}, self.queryset).qs.count(), 1)


class DHCPScopeFilterSetTests(TestCase):
    queryset = DHCPScope.objects.all()
    filterset = DHCPScopeFilterSet

    @classmethod
    def setUpTestData(cls):
        cls.server = make_server()
        make_scope(name='Building A', prefix=make_prefix('10.0.1.0/24'), server=cls.server)
        make_scope(
            name='Building B', prefix=make_prefix('10.0.2.0/24'),
            server=make_server(name='Srv2', hostname='srv2.example.com'),
            start_ip='10.0.2.10', end_ip='10.0.2.254',
        )

    def test_q_matches_name(self):
        self.assertEqual(self.filterset({'q': 'Building A'}, self.queryset).qs.count(), 1)

    def test_server_id(self):
        self.assertEqual(
            self.filterset({'server_id': [self.server.pk]}, self.queryset).qs.count(), 1
        )

    def test_within_prefix(self):
        # Both scopes are inside 10.0.0.0/16.
        self.assertEqual(
            self.filterset({'within_prefix': '10.0.0.0/16'}, self.queryset).qs.count(), 2
        )
        # Only Building A is inside 10.0.1.0/24.
        self.assertEqual(
            self.filterset({'within_prefix': '10.0.1.0/24'}, self.queryset).qs.count(), 1
        )
