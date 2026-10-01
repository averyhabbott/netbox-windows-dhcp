"""
Management commands: dhcp_apply_prefix_tenant and dhcp_fix_ip_vrf.
"""

from io import StringIO

from core.models import ObjectChange
from django.core.management import call_command
from django.test import TestCase
from extras.models import Tag
from ipam.models import IPAddress, VRF

from ..background_tasks import _sync_scope_ips
from .base import NULL_LOGGER, make_prefix, make_scope, make_server, managed_ip, set_plugin_settings


class ApplyPrefixTenantTests(TestCase):
    """New IPs inherit the scope prefix's tenant; sync never re-tenants existing IPs;
    dhcp_apply_prefix_tenant backfills on demand."""

    @classmethod
    def setUpTestData(cls):
        from tenancy.models import Tenant
        cls.tenant = Tenant.objects.create(name='Tenant A', slug='tenant-a')
        cls.other = Tenant.objects.create(name='Tenant B', slug='tenant-b')
        cls.scope = make_scope(prefix=make_prefix('10.0.1.0/24', tenant=cls.tenant))

    def _run(self, leases=None, reservations=None, **kwargs):
        _sync_scope_ips(NULL_LOGGER, self.scope, leases=leases or [],
                        reservations=reservations or [], **kwargs)

    def _backfill(self, *args):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command('dhcp_apply_prefix_tenant', *args, stdout=out)
        return out.getvalue()


    # --- dhcp_apply_prefix_tenant ---

    def test_backfill_fills_blank_tenant(self):
        ip = managed_ip('10.0.1.50/24')
        self._backfill()
        ip.refresh_from_db()
        self.assertEqual(ip.tenant, self.tenant)

    def test_backfill_leaves_different_tenant_without_overwrite(self):
        ip = managed_ip('10.0.1.50/24', tenant=self.other)
        self._backfill()
        ip.refresh_from_db()
        self.assertEqual(ip.tenant, self.other)

    def test_backfill_overwrite_replaces_different_tenant(self):
        ip = managed_ip('10.0.1.50/24', tenant=self.other)
        self._backfill('--overwrite')
        ip.refresh_from_db()
        self.assertEqual(ip.tenant, self.tenant)

    def test_backfill_dry_run_changes_nothing(self):
        ip = managed_ip('10.0.1.50/24')
        out = self._backfill('--dry-run')
        ip.refresh_from_db()
        self.assertIsNone(ip.tenant)
        self.assertIn('10.0.1.50', out)

    def test_backfill_skips_unmanaged_ips(self):
        manual = IPAddress.objects.create(address='10.0.1.60/24', status='reserved')  # no lease info
        other_status = IPAddress.objects.create(address='10.0.1.61/24', status='active')
        self._backfill()
        manual.refresh_from_db()
        other_status.refresh_from_db()
        self.assertIsNone(manual.tenant)
        self.assertIsNone(other_status.tenant)

    def test_backfill_skips_protected_ip(self):
        tag = Tag.objects.create(name='Protect', slug='protect')
        set_plugin_settings(sync_protect_tag=tag)
        ip = managed_ip('10.0.1.50/24')
        ip.tags.add(tag)
        self._backfill()
        ip.refresh_from_db()
        self.assertIsNone(ip.tenant)

    def test_backfill_skips_ip_in_protected_prefix(self):
        tag = Tag.objects.create(name='Protect', slug='protect')
        set_plugin_settings(sync_protect_tag=tag)
        make_prefix('10.0.1.0/28').tags.add(tag)
        protected = managed_ip('10.0.1.5/24')
        unprotected = managed_ip('10.0.1.50/24')
        self._backfill()
        protected.refresh_from_db()
        unprotected.refresh_from_db()
        self.assertIsNone(protected.tenant)
        self.assertEqual(unprotected.tenant, self.tenant)


class ApplyPrefixTenantVRFTests(TestCase):

    def test_only_ips_in_the_prefix_vrf_are_retenanted(self):
        from tenancy.models import Tenant
        tenant = Tenant.objects.create(name='Tenant A', slug='tenant-a')
        red = VRF.objects.create(name='Red')
        make_scope(prefix=make_prefix('10.0.1.0/24', vrf=red, tenant=tenant))
        red_ip = managed_ip('10.0.1.50/24', vrf=red)
        global_ip = managed_ip('10.0.1.50/24')
        call_command('dhcp_apply_prefix_tenant', stdout=StringIO())
        red_ip.refresh_from_db()
        global_ip.refresh_from_db()
        self.assertEqual(red_ip.tenant, tenant)
        self.assertIsNone(global_ip.tenant)


class FixIpVrfCommandTests(TestCase):

    def setUp(self):
        self.red = VRF.objects.create(name='Red')
        self.server = make_server()
        self.scope = make_scope(name='Red', prefix=make_prefix('10.0.1.0/24', vrf=self.red), server=self.server)

    def _run(self, *args):
        out = StringIO()
        call_command('dhcp_fix_ip_vrf', *args, stdout=out)
        return out.getvalue()

    def test_moves_sync_made_ip_into_prefix_vrf(self):
        tag = Tag.objects.create(name='Keep', slug='keep')
        ip = managed_ip('10.0.1.50/24', status='reserved')
        ip.tags.add(tag)
        self._run()
        ip.refresh_from_db()
        self.assertEqual(ip.vrf, self.red)
        self.assertIn(tag, ip.tags.all())  # same object: tags, history, assignments carry over

    def test_change_is_logged_as_sync_service(self):
        ip = managed_ip('10.0.1.50/24')
        self._run()
        change = ObjectChange.objects.filter(changed_object_id=ip.pk).latest('time')
        self.assertEqual(change.user.username, 'DHCP-Sync-Service')

    def test_dry_run_changes_nothing(self):
        ip = managed_ip('10.0.1.50/24')
        out = self._run('--dry-run')
        ip.refresh_from_db()
        self.assertIsNone(ip.vrf)
        self.assertIn('10.0.1.50', out)

    def test_hand_made_ips_are_untouched(self):
        no_lease_info = IPAddress.objects.create(address='10.0.1.50/24', status='reserved')
        other_status = managed_ip('10.0.1.51/24', status='active')
        self._run()
        no_lease_info.refresh_from_db()
        other_status.refresh_from_db()
        self.assertIsNone(no_lease_info.vrf)
        self.assertIsNone(other_status.vrf)

    def test_skips_protected_ip_and_ip_in_protected_prefix(self):
        tag = Tag.objects.create(name='Protect', slug='protect')
        set_plugin_settings(sync_protect_tag=tag)
        tagged = managed_ip('10.0.1.50/24')
        tagged.tags.add(tag)
        make_prefix('10.0.1.0/28').tags.add(tag)
        in_protected_prefix = managed_ip('10.0.1.5/24')
        self._run()
        tagged.refresh_from_db()
        in_protected_prefix.refresh_from_db()
        self.assertIsNone(tagged.vrf)
        self.assertIsNone(in_protected_prefix.vrf)

    def test_skips_when_address_already_exists_in_vrf(self):
        IPAddress.objects.create(address='10.0.1.50/24', status='active', vrf=self.red)
        ip = managed_ip('10.0.1.50/24')
        out = self._run()
        ip.refresh_from_db()
        self.assertIsNone(ip.vrf)
        self.assertIn('10.0.1.50', out)

    def test_leaves_ip_inside_a_no_vrf_scope_prefix_alone(self):
        make_scope(name='Global', prefix=make_prefix('10.0.1.0/24'), server=self.server)
        ip = managed_ip('10.0.1.50/24')
        self._run()
        ip.refresh_from_db()
        self.assertIsNone(ip.vrf)

    def test_skips_ip_inside_scope_prefixes_in_more_than_one_vrf(self):
        blue = VRF.objects.create(name='Blue')
        make_scope(name='Blue', prefix=make_prefix('10.0.1.0/24', vrf=blue), server=self.server)
        ip = managed_ip('10.0.1.50/24')
        out = self._run()
        ip.refresh_from_db()
        self.assertIsNone(ip.vrf)
        self.assertIn('10.0.1.50', out)

    def test_ignores_ips_outside_any_scope_prefix(self):
        ip = managed_ip('10.9.9.9/24')
        self._run()
        ip.refresh_from_db()
        self.assertIsNone(ip.vrf)
