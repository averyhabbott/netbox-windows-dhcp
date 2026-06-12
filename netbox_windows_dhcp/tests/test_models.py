"""Model validation and behavior tests — DB only, no network."""

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from ..models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPPluginSettings,
    DHCPScope,
)
from .base import (
    make_failover,
    make_option_definition,
    make_prefix,
    make_scope,
    make_server,
)


class DHCPFailoverCleanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.primary = make_server(name='P', hostname='p.example.com')
        cls.secondary = make_server(name='S', hostname='s.example.com')

    def test_same_primary_and_secondary_rejected(self):
        fo = DHCPFailover(name='FO', primary_server=self.primary, secondary_server=self.primary)
        with self.assertRaises(ValidationError):
            fo.clean()

    def test_auth_without_secret_rejected(self):
        fo = DHCPFailover(
            name='FO', primary_server=self.primary, secondary_server=self.secondary,
            enable_auth=True, shared_secret='',
        )
        with self.assertRaises(ValidationError):
            fo.clean()

    def test_auth_with_secret_ok(self):
        fo = DHCPFailover(
            name='FO', primary_server=self.primary, secondary_server=self.secondary,
            enable_auth=True, shared_secret='s3cret',
        )
        fo.clean()  # should not raise

    def test_distinct_servers_ok(self):
        fo = DHCPFailover(name='FO', primary_server=self.primary, secondary_server=self.secondary)
        fo.clean()  # should not raise


class DHCPScopeCleanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.prefix = make_prefix('10.0.1.0/24')
        cls.server = make_server()
        cls.failover = make_failover()

    def test_both_server_and_failover_rejected(self):
        scope = DHCPScope(
            name='S', prefix=self.prefix, server=self.server, failover=self.failover,
            start_ip='10.0.1.10', end_ip='10.0.1.20',
        )
        with self.assertRaises(ValidationError):
            scope.clean()

    def test_neither_server_nor_failover_rejected(self):
        scope = DHCPScope(
            name='S', prefix=self.prefix, start_ip='10.0.1.10', end_ip='10.0.1.20',
        )
        with self.assertRaises(ValidationError):
            scope.clean()

    def test_start_ip_outside_prefix_rejected(self):
        scope = DHCPScope(
            name='S', prefix=self.prefix, server=self.server,
            start_ip='10.9.9.10', end_ip='10.0.1.20',
        )
        with self.assertRaises(ValidationError):
            scope.clean()

    def test_end_before_start_rejected(self):
        scope = DHCPScope(
            name='S', prefix=self.prefix, server=self.server,
            start_ip='10.0.1.200', end_ip='10.0.1.10',
        )
        with self.assertRaises(ValidationError):
            scope.clean()

    def test_valid_server_scope_ok(self):
        scope = DHCPScope(
            name='S', prefix=self.prefix, server=self.server,
            start_ip='10.0.1.10', end_ip='10.0.1.254',
        )
        scope.clean()  # should not raise

    def test_valid_failover_scope_ok(self):
        scope = DHCPScope(
            name='S', prefix=self.prefix, failover=self.failover,
            start_ip='10.0.1.10', end_ip='10.0.1.254',
        )
        scope.clean()  # should not raise


class DHCPExclusionRangeCleanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.scope = make_scope()  # prefix 10.0.1.0/24

    def test_end_before_start_rejected(self):
        ex = DHCPExclusionRange(scope=self.scope, start_ip='10.0.1.50', end_ip='10.0.1.40')
        with self.assertRaises(ValidationError):
            ex.clean()

    def test_start_outside_prefix_rejected(self):
        ex = DHCPExclusionRange(scope=self.scope, start_ip='10.9.9.1', end_ip='10.0.1.40')
        with self.assertRaises(ValidationError):
            ex.clean()

    def test_end_outside_prefix_rejected(self):
        ex = DHCPExclusionRange(scope=self.scope, start_ip='10.0.1.10', end_ip='10.9.9.40')
        with self.assertRaises(ValidationError):
            ex.clean()

    def test_valid_range_ok(self):
        ex = DHCPExclusionRange(scope=self.scope, start_ip='10.0.1.50', end_ip='10.0.1.60')
        ex.clean()  # should not raise

    def test_malformed_ip_returns_quietly(self):
        # clean() bails out (returns) rather than raising on unparseable IPs.
        ex = DHCPExclusionRange(scope=self.scope, start_ip='not-an-ip', end_ip='also-bad')
        ex.clean()  # should not raise


class DHCPOptionCodeDefinitionTests(TestCase):
    def test_builtin_cannot_be_deleted(self):
        opt = make_option_definition(code=200, name='Router', is_builtin=True)
        with self.assertRaises(ValidationError):
            opt.delete()
        self.assertTrue(DHCPOptionCodeDefinition.objects.filter(pk=opt.pk).exists())

    def test_non_builtin_can_be_deleted(self):
        opt = make_option_definition(code=201, name='Custom', is_builtin=False)
        opt.delete()
        self.assertFalse(DHCPOptionCodeDefinition.objects.filter(pk=opt.pk).exists())


class DHCPOptionValueStrTests(TestCase):
    def test_str_prefers_friendly_name(self):
        opt = make_option_definition(code=200, name='DNS')
        val = DHCPOptionValue(option_definition=opt, value='10.0.0.1', friendly_name='Primary DNS')
        self.assertEqual(str(val), 'Primary DNS')

    def test_str_falls_back_to_code_and_value(self):
        opt = make_option_definition(code=200, name='DNS')
        val = DHCPOptionValue(option_definition=opt, value='10.0.0.1', friendly_name='')
        self.assertEqual(str(val), '200: 10.0.0.1')


class DHCPPluginSettingsTests(TestCase):
    def test_load_is_singleton(self):
        a = DHCPPluginSettings.load()
        b = DHCPPluginSettings.load()
        self.assertEqual(a.pk, 1)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(DHCPPluginSettings.objects.count(), 1)

    @override_settings(PLUGINS_CONFIG={'netbox_windows_dhcp': {
        'sync_ips_from_dhcp': True,
        'push_reservations': True,
        'push_scope_info': True,
    }})
    def test_plugins_config_overrides_win(self):
        # Persist False in the DB, but the in-memory override should report True.
        DHCPPluginSettings.objects.update_or_create(
            pk=1, defaults={'sync_ip_addresses': False, 'push_reservations': False, 'push_scope_info': False},
        )
        settings_obj = DHCPPluginSettings.load()
        self.assertTrue(settings_obj.sync_ip_addresses)
        self.assertTrue(settings_obj.push_reservations)
        self.assertTrue(settings_obj.push_scope_info)

    def test_db_value_used_without_override(self):
        DHCPPluginSettings.objects.update_or_create(
            pk=1, defaults={'sync_ip_addresses': True},
        )
        self.assertTrue(DHCPPluginSettings.load().sync_ip_addresses)


class GetAbsoluteUrlSmokeTests(TestCase):
    """Each model resolves its detail URL without error."""

    def test_urls_resolve(self):
        server = make_server()
        failover = make_failover()
        scope = make_scope()
        opt_def = make_option_definition()
        opt_val = DHCPOptionValue.objects.create(option_definition=opt_def, value='1.2.3.4')
        ex = DHCPExclusionRange.objects.create(scope=scope, start_ip='10.0.1.50', end_ip='10.0.1.60')
        for obj in (server, failover, scope, opt_def, opt_val, ex):
            self.assertIn('/plugins/windows-dhcp/', obj.get_absolute_url(), msg=repr(obj))
