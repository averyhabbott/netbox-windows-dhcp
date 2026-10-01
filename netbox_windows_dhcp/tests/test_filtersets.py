"""
Finding things: the list filters (the same ones the API's query parameters use), the
"Unassigned Scopes" saved filter, and global search.

NetBox's ChangeLoggedFilterSetTests isn't used: it expects a filter for every model
field, and these filtersets offer a chosen few.
"""

from django.test import TestCase
from extras.models import SavedFilter

from .. import _ensure_unassigned_scopes_filter
from ..filtersets import (
    DHCPExclusionRangeFilterSet,
    DHCPFailoverFilterSet,
    DHCPOptionCodeDefinitionFilterSet,
    DHCPOptionValueFilterSet,
    DHCPScopeFilterSet,
    DHCPServerFilterSet,
)
from ..models import DHCPExclusionRange, DHCPOptionCodeDefinition, DHCPOptionValue, DHCPServer
from ..search import DHCPScopeIndex
from .base import (
    clear_builtin_option_codes,
    make_failover,
    make_prefix,
    make_scope,
    make_server,
    make_unassigned_scope,
)


class _FilterCases:
    filterset = None

    def assertFinds(self, cases):
        queryset = self.filterset.Meta.model.objects.all()
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual(self.filterset(params, queryset).qs.count(), expected)


class ServerFilterTests(_FilterCases, TestCase):
    filterset = DHCPServerFilterSet

    @classmethod
    def setUpTestData(cls):
        make_server(name='Alpha DHCP', hostname='alpha.example.com', port=443)
        make_server(name='Bravo DHCP', hostname='bravo.example.com', port=8443)
        make_server(name='Charlie', hostname='charlie.example.com', port=443)
        DHCPServer.objects.filter(name='Alpha DHCP').update(health_status='healthy', access_level='ro')
        DHCPServer.objects.filter(name='Bravo DHCP').update(health_status='unreachable')

    def test_filters(self):
        self.assertFinds((
            ({'q': 'Alpha'}, 1),
            ({'q': 'bravo.example'}, 1),
            ({'name': 'dhcp'}, 2),
            ({'port': [443]}, 2),
            ({'health_status': ['healthy']}, 1),
            ({'health_status': ['healthy', 'unreachable']}, 2),
            ({'access_level': ['ro']}, 1),
            ({'access_level': ['unknown']}, 2),
        ))


class FailoverFilterTests(_FilterCases, TestCase):
    filterset = DHCPFailoverFilterSet

    @classmethod
    def setUpTestData(cls):
        cls.p = make_server(name='P', hostname='p.example.com')
        make_failover(name='FO Load', primary=cls.p, secondary=make_server(name='S', hostname='s.example.com'),
                      mode='LoadBalance')
        make_failover(name='FO Hot', primary=make_server(name='P2', hostname='p2.example.com'),
                      secondary=make_server(name='S2', hostname='s2.example.com'), mode='HotStandby')

    def test_filters(self):
        self.assertFinds((
            ({'q': 'Load'}, 1),
            ({'mode': ['HotStandby']}, 1),
            ({'primary_server_id': [self.p.pk]}, 1),
        ))


class OptionCodeFilterTests(_FilterCases, TestCase):
    filterset = DHCPOptionCodeDefinitionFilterSet

    @classmethod
    def setUpTestData(cls):
        clear_builtin_option_codes()  # start from an empty table for deterministic counts
        DHCPOptionCodeDefinition.objects.create(code=200, name='ZZ-TFTP Server')
        DHCPOptionCodeDefinition.objects.create(code=201, name='ZZ-Bootfile', is_builtin=True)
        DHCPOptionCodeDefinition.objects.create(code=202, name='ZZ-Cisco', vendor_class='Cisco')

    def test_filters(self):
        self.assertFinds((
            ({'q': 'ZZ-TFTP'}, 1),
            ({'q': '200'}, 1),
            ({'is_builtin': True}, 1),
        ))


class OptionValueFilterTests(_FilterCases, TestCase):
    filterset = DHCPOptionValueFilterSet

    @classmethod
    def setUpTestData(cls):
        opt = DHCPOptionCodeDefinition.objects.create(code=200, name='DNS')
        DHCPOptionValue.objects.create(option_definition=opt, value='10.0.0.1', friendly_name='Primary DNS')
        DHCPOptionValue.objects.create(option_definition=opt, value='10.0.0.2', friendly_name='Secondary DNS')
        DHCPOptionValue.objects.create(option_definition=opt, value='8.8.8.8', friendly_name='Public')
        tftp = DHCPOptionCodeDefinition.objects.create(code=250, name='TFTP')
        DHCPOptionValue.objects.create(option_definition=tftp, value='10.0.0.9')

    def test_filters(self):
        self.assertFinds((
            ({'q': 'Primary'}, 1),
            ({'q': '8.8.8.8'}, 1),
            ({'q': '250'}, 1),  # the code alone finds its values
            ({'value': '10.0.0'}, 3),
        ))


class ExclusionRangeFilterTests(_FilterCases, TestCase):
    filterset = DHCPExclusionRangeFilterSet

    @classmethod
    def setUpTestData(cls):
        cls.scope = make_scope()
        DHCPExclusionRange.objects.create(scope=cls.scope, start_ip='10.0.1.50', end_ip='10.0.1.60')
        DHCPExclusionRange.objects.create(scope=cls.scope, start_ip='10.0.1.70', end_ip='10.0.1.80')

    def test_filters(self):
        self.assertFinds((
            ({'scope_id': [self.scope.pk]}, 2),
            ({'q': '10.0.1.50'}, 1),
        ))


class ScopeFilterTests(_FilterCases, TestCase):
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
        make_unassigned_scope(name='Unassigned', network='10.0.3.0', server=cls.server,
                              start_ip='10.0.3.10', end_ip='10.0.3.20')

    def test_filters(self):
        self.assertFinds((
            ({'q': 'Building A'}, 1),
            ({'server_id': [self.server.pk]}, 2),
            ({'within_prefix': '10.0.0.0/16'}, 2),
            ({'within_prefix': '10.0.1.0/24'}, 1),
            ({'has_prefix': 'true'}, 2),
            ({'has_prefix': 'false'}, 1),
        ))


class UnassignedScopesSavedFilterTests(TestCase):

    def setUp(self):
        SavedFilter.objects.filter(slug='unassigned-scopes').delete()

    def test_created(self):
        _ensure_unassigned_scopes_filter(sender=None)
        saved_filter = SavedFilter.objects.get(slug='unassigned-scopes')
        self.assertEqual(saved_filter.name, 'Unassigned Scopes')
        self.assertEqual(saved_filter.parameters, {'has_prefix': ['false']})
        self.assertTrue(saved_filter.shared)
        self.assertEqual(
            list(saved_filter.object_types.values_list('app_label', 'model')),
            [('netbox_windows_dhcp', 'dhcpscope')],
        )

    def test_an_existing_filter_with_that_name_is_left_alone(self):
        SavedFilter.objects.create(name='Unassigned Scopes', slug='my-own', parameters={'x': ['1']})
        _ensure_unassigned_scopes_filter(sender=None)
        self.assertEqual(SavedFilter.objects.filter(name='Unassigned Scopes').count(), 1)
        self.assertEqual(SavedFilter.objects.get(name='Unassigned Scopes').parameters, {'x': ['1']})


class SearchTests(TestCase):

    def test_scopes_are_found_by_network(self):
        scope = make_scope()  # prefix 10.0.1.0/24
        self.assertEqual(DHCPScopeIndex.get_field_value(scope, 'prefix'), '10.0.1.0/24')
