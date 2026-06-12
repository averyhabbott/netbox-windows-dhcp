"""Search-index registration tests."""

from django.test import TestCase

from ..models import DHCPLeaseInfo, DHCPScope, DHCPServer
from ..search import (
    DHCPLeaseInfoIndex,
    DHCPScopeIndex,
    DHCPServerIndex,
    indexes,
)
from .base import make_scope


class SearchIndexTests(TestCase):
    def test_all_three_indexes_registered(self):
        self.assertEqual(set(indexes), {DHCPLeaseInfoIndex, DHCPServerIndex, DHCPScopeIndex})

    def test_index_models(self):
        self.assertIs(DHCPServerIndex.model, DHCPServer)
        self.assertIs(DHCPScopeIndex.model, DHCPScope)
        self.assertIs(DHCPLeaseInfoIndex.model, DHCPLeaseInfo)

    def test_scope_fields_include_name_and_prefix(self):
        field_names = {f[0] for f in DHCPScopeIndex.fields}
        self.assertIn('name', field_names)
        self.assertIn('prefix', field_names)

    def test_prefix_indexed_as_cidr_string(self):
        scope = make_scope(prefix=None)  # default prefix 10.0.1.0/24
        value = DHCPScopeIndex.get_field_value(scope, 'prefix')
        self.assertEqual(value, '10.0.1.0/24')
