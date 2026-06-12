"""
Signal-handler tests. Job enqueue methods are patched so no test reaches RQ/Redis.
"""

from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase
from ipam.models import IPAddress

from ..models import DHCPExclusionRange
from ..signals import validate_dhcp_ip_status
from .base import make_scope, make_server, set_plugin_settings

ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPServerSyncJob.enqueue'
ENQUEUE_ONCE = 'netbox_windows_dhcp.background_tasks.DHCPSyncJob.enqueue_once'


class ScopePostSaveSignalTests(TestCase):
    def test_enqueues_server_sync_when_push_enabled(self):
        server = make_server()
        with mock.patch(ENQUEUE_ONCE), mock.patch(ENQUEUE) as enq:
            set_plugin_settings(push_scope_info=True)
            make_scope(server=server)
        self.assertTrue(enq.called)

    def test_no_enqueue_when_push_disabled(self):
        server = make_server()
        with mock.patch(ENQUEUE_ONCE), mock.patch(ENQUEUE) as enq:
            set_plugin_settings(push_scope_info=False)
            make_scope(server=server)
        self.assertFalse(enq.called)


class SettingsPostSaveSignalTests(TestCase):
    def test_reschedules_sync_with_new_interval(self):
        with mock.patch(ENQUEUE_ONCE) as enqueue_once:
            set_plugin_settings(sync_interval=120)
        self.assertTrue(enqueue_once.called)
        self.assertEqual(enqueue_once.call_args.kwargs.get('interval'), 120)


class ValidateDHCPIPStatusTests(TestCase):
    """The post_clean handler is called directly with constructed instances."""

    def setUp(self):
        set_plugin_settings(lease_status='dhcp')

    # IPs are saved AND reloaded so the address field is coerced to a netaddr
    # object — the handler reads instance.address.ip and silently bails on an
    # uncoerced (str) value, which a freshly-created in-memory instance still has.
    # Saving does not trigger validation (post_clean only fires on full_clean).
    @staticmethod
    def _make_ip(address, status):
        ip = IPAddress.objects.create(address=address, status=status)
        ip.refresh_from_db()
        return ip

    def test_non_lease_status_is_ignored(self):
        ip = self._make_ip('10.0.1.50/24', 'active')
        validate_dhcp_ip_status(sender=IPAddress, instance=ip)  # no raise

    def test_lease_status_without_scope_raises(self):
        ip = self._make_ip('10.0.1.50/24', 'dhcp')
        with self.assertRaises(ValidationError):
            validate_dhcp_ip_status(sender=IPAddress, instance=ip)

    def test_lease_status_within_scope_ok(self):
        make_scope(start_ip='10.0.1.10', end_ip='10.0.1.254')  # prefix 10.0.1.0/24
        ip = self._make_ip('10.0.1.60/24', 'dhcp')
        validate_dhcp_ip_status(sender=IPAddress, instance=ip)  # no raise

    def test_lease_status_inside_exclusion_raises(self):
        scope = make_scope(start_ip='10.0.1.10', end_ip='10.0.1.254')
        DHCPExclusionRange.objects.create(scope=scope, start_ip='10.0.1.40', end_ip='10.0.1.60')
        ip = self._make_ip('10.0.1.50/24', 'dhcp')
        with self.assertRaises(ValidationError):
            validate_dhcp_ip_status(sender=IPAddress, instance=ip)
