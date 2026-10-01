"""
What NetBox accepts as a failover, a scope or an exclusion range, which built-in option
codes can't be deleted, and where the plugin settings come from. DB only, no network.
"""

from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings

from ..models import DHCPExclusionRange, DHCPFailover, DHCPOptionCodeDefinition, DHCPPluginSettings, DHCPScope
from .base import (
    make_failover,
    make_option_definition,
    make_prefix,
    make_scope,
    make_server,
    make_unassigned_scope,
)


class FailoverValidationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.primary = make_server(name='P', hostname='p.example.com')
        cls.secondary = make_server(name='S', hostname='s.example.com')

    def test_refused(self):
        cases = {
            'same primary and secondary': dict(secondary_server=self.primary),
            'auth without a secret': dict(enable_auth=True, shared_secret=''),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                kwargs.setdefault('secondary_server', self.secondary)
                with self.assertRaises(ValidationError):
                    DHCPFailover(name='FO', primary_server=self.primary, **kwargs).clean()

    def test_accepted(self):
        DHCPFailover(name='FO', primary_server=self.primary, secondary_server=self.secondary).clean()
        DHCPFailover(name='FO', primary_server=self.primary, secondary_server=self.secondary,
                     enable_auth=True, shared_secret='s3cret').clean()


class ScopeValidationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.prefix = make_prefix('10.0.1.0/24')
        cls.server = make_server()
        cls.failover = make_failover()

    def test_refused(self):
        cases = {
            'both a server and a failover': dict(server=self.server, failover=self.failover),
            'neither a server nor a failover': dict(),
            'start outside the prefix': dict(server=self.server, start_ip='10.9.9.10'),
            'end before start': dict(server=self.server, start_ip='10.0.1.200', end_ip='10.0.1.10'),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                kwargs.setdefault('start_ip', '10.0.1.10')
                kwargs.setdefault('end_ip', '10.0.1.20')
                with self.assertRaises(ValidationError):
                    DHCPScope(name='S', prefix=self.prefix, **kwargs).clean()

    def test_accepted(self):
        for owner in (dict(server=self.server), dict(failover=self.failover)):
            DHCPScope(name='S', prefix=self.prefix, start_ip='10.0.1.10', end_ip='10.0.1.254', **owner).clean()


class ScopeWithoutPrefixTests(TestCase):
    """A scope with no prefix stores its own network and prefix length."""

    def setUp(self):
        self.server = make_server()

    def _scope(self, **kwargs):
        kwargs.setdefault('start_ip', '10.0.1.10')
        kwargs.setdefault('end_ip', '10.0.1.20')
        return DHCPScope(name='X', server=self.server, **kwargs)

    def test_network_follows_the_prefix(self):
        prefix = make_prefix('10.0.1.0/24')
        scope = make_scope(prefix=prefix, server=self.server)
        self.assertEqual((scope.network, scope.prefix_length), ('10.0.1.0', 24))
        self.assertEqual(str(scope.network_cidr), '10.0.1.0/24')
        prefix.prefix = '10.0.0.0/23'
        prefix.save()
        scope.refresh_from_db()
        self.assertEqual((scope.network, scope.prefix_length), ('10.0.0.0', 23))

    def test_refused(self):
        cases = {
            'no prefix and no network': dict(),
            'not the network address': dict(network='10.0.1.5', prefix_length=24),
            'range outside the network': dict(network='10.0.1.0', prefix_length=24, start_ip='10.0.2.10'),
            'a prefix and a different network': dict(prefix=make_prefix('10.0.1.0/24'), network='10.0.2.0',
                                                     prefix_length=24),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValidationError):
                    self._scope(**kwargs).full_clean()

    def test_accepted(self):
        self._scope(network='10.0.1.0', prefix_length=24).full_clean()

    def test_linking_a_prefix_later(self):
        scope = make_unassigned_scope(server=self.server)
        for cidr in ('10.0.2.0/24', '10.0.0.0/23'):
            with self.subTest(cidr=cidr):
                scope.prefix = make_prefix(cidr)
                with self.assertRaises(ValidationError):
                    scope.full_clean()
        scope.refresh_from_db()
        scope.prefix = make_prefix('10.0.1.0/24')
        scope.full_clean()
        scope.save()
        scope.refresh_from_db()
        self.assertIsNotNone(scope.prefix_id)

    def test_exclusion_checked_against_the_stored_network(self):
        scope = make_unassigned_scope(server=self.server)
        DHCPExclusionRange(scope=scope, start_ip='10.0.1.20', end_ip='10.0.1.30').full_clean()
        with self.assertRaises(ValidationError):
            DHCPExclusionRange(scope=scope, start_ip='10.0.2.20', end_ip='10.0.2.30').full_clean()


class ScopeUniquenessTests(TestCase):
    """One scope per prefix, and one scope per network on a server."""

    def setUp(self):
        self.primary = make_server(name='Primary', hostname='primary')
        self.secondary = make_server(name='Secondary', hostname='secondary')
        self.failover = make_failover(primary=self.primary, secondary=self.secondary)

    def _scope(self, **kwargs):
        kwargs.setdefault('name', 'New')
        kwargs.setdefault('start_ip', '10.0.1.10')
        kwargs.setdefault('end_ip', '10.0.1.20')
        if 'prefix' not in kwargs:
            kwargs.setdefault('network', '10.0.1.0')
            kwargs.setdefault('prefix_length', 24)
        return DHCPScope(**kwargs)

    def test_second_scope_on_a_prefix_is_refused(self):
        prefix = make_prefix('10.0.1.0/24')
        make_scope(name='First', prefix=prefix, server=self.primary)
        other = make_server(name='Other', hostname='other')
        with self.assertRaisesRegex(ValidationError, '"First"'):
            self._scope(prefix=prefix, server=other).full_clean()
        # Existing data that breaks the rule can't be saved again either.
        second = make_scope(name='Second', prefix=prefix, server=make_server(name='O', hostname='o'))
        with self.assertRaises(ValidationError):
            second.full_clean()

    def test_same_network_twice_on_one_server_is_refused(self):
        cases = {
            'standalone, then standalone': (dict(server=self.primary), dict(server=self.primary)),
            'failover, then standalone on the primary': (dict(failover=self.failover), dict(server=self.primary)),
            'failover, then standalone on the secondary': (dict(failover=self.failover), dict(server=self.secondary)),
            'standalone, then failover': (dict(server=self.secondary), dict(failover=self.failover)),
        }
        for label, (first, second) in cases.items():
            with self.subTest(label):
                existing = make_unassigned_scope(name='Existing', start_ip='10.0.1.10', end_ip='10.0.1.20', **first)
                with self.assertRaises(ValidationError):
                    self._scope(**second).full_clean()
                existing.delete()

    def test_same_network_on_different_servers_is_fine(self):
        make_unassigned_scope(name='Existing', start_ip='10.0.1.10', end_ip='10.0.1.20', server=self.primary)
        self._scope(server=make_server(name='Prod', hostname='prod')).full_clean()


class ExclusionRangeValidationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scope = make_scope()  # prefix 10.0.1.0/24

    def test_refused(self):
        for start, end in (('10.0.1.50', '10.0.1.40'), ('10.9.9.1', '10.0.1.40'), ('10.0.1.10', '10.9.9.40')):
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValidationError):
                    DHCPExclusionRange(scope=self.scope, start_ip=start, end_ip=end).clean()

    def test_accepted(self):
        DHCPExclusionRange(scope=self.scope, start_ip='10.0.1.50', end_ip='10.0.1.60').clean()


class OptionCodeTests(TestCase):

    def test_builtin_cannot_be_deleted(self):
        opt = make_option_definition(code=200, name='Router', is_builtin=True)
        with self.assertRaises(ProtectedError):
            opt.delete()
        self.assertTrue(DHCPOptionCodeDefinition.objects.filter(pk=opt.pk).exists())


class PluginSettingsTests(TestCase):

    def test_there_is_only_ever_one(self):
        self.assertEqual(DHCPPluginSettings.load().pk, DHCPPluginSettings.load().pk)
        self.assertEqual(DHCPPluginSettings.objects.count(), 1)

    @override_settings(PLUGINS_CONFIG={'netbox_windows_dhcp': {
        'sync_ips_from_dhcp': True,
        'push_reservations': True,
        'push_scope_info': True,
    }})
    def test_plugins_config_overrides_win(self):
        DHCPPluginSettings.objects.update_or_create(
            pk=1, defaults={'sync_ip_addresses': False, 'push_reservations': False, 'push_scope_info': False},
        )
        settings_obj = DHCPPluginSettings.load()
        self.assertTrue(settings_obj.sync_ip_addresses)
        self.assertTrue(settings_obj.push_reservations)
        self.assertTrue(settings_obj.push_scope_info)
