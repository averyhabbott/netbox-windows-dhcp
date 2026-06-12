"""
Shared fixtures and helpers for the netbox-windows-dhcp test suite.

Two kinds of helper live here:

1. Model fixture builders (``make_server`` etc.) used by ``setUpTestData`` across
   the model/api/view/filterset tests.
2. A ``FakePSUClient`` plus canned PSU payloads, used by the import and sync tests
   so that **no test ever opens a network connection or talks to a real PSU
   server**. The plugin's import/sync helpers accept a ``client`` argument, so the
   fake is simply passed in.
"""

import logging

from ipam.models import Prefix

from ..models import (
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPPluginSettings,
    DHCPScope,
    DHCPServer,
)

# A throwaway logger for the sync helpers, which expect a `job_logger` argument.
NULL_LOGGER = logging.getLogger('netbox_windows_dhcp.tests')
NULL_LOGGER.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# Namespace mixins for the NetBox test harness
# ---------------------------------------------------------------------------

class PluginAPIViewTestMixin:
    """
    Point the NetBox APIViewTestCases harness at the plugin's API namespace.

    NetBox's APITestCase builds URLs as ``{view_namespace}-api:{model}-detail``,
    defaulting view_namespace to the model's app_label. Plugin API views live
    under the ``plugins-api:`` prefix, so we set it explicitly.
    """

    view_namespace = 'plugins-api:netbox_windows_dhcp'


class PluginViewTestMixin:
    """Point the NetBox ViewTestCases harness at the plugin's UI URL namespace."""

    def _get_base_url(self):
        return f'plugins:{self.model._meta.app_label}:{self.model._meta.model_name}_{{}}'


# ---------------------------------------------------------------------------
# Settings helper
# ---------------------------------------------------------------------------

def clear_builtin_option_codes():
    """
    Remove the built-in DHCPOptionCodeDefinition rows seeded by migration 0002.

    Uses a queryset delete (raw SQL) which bypasses the model's delete() guard on
    is_builtin rows. Call from setUpTestData in option-code list/bulk-delete tests
    so the table starts empty and counts are deterministic. Safe within a test's
    transaction — it does not affect the real database.
    """
    DHCPOptionCodeDefinition.objects.all().delete()


def set_plugin_settings(**kwargs):
    """Load the singleton settings, apply kwargs, save, and return it."""
    settings_obj = DHCPPluginSettings.load()
    for key, value in kwargs.items():
        setattr(settings_obj, key, value)
    settings_obj.save()
    return settings_obj


# ---------------------------------------------------------------------------
# Model fixture builders
# ---------------------------------------------------------------------------

def make_prefix(cidr='10.0.1.0/24', **kwargs):
    obj, _ = Prefix.objects.get_or_create(prefix=cidr, defaults={'status': 'active', **kwargs})
    # Reload so .prefix is a netaddr IPNetwork (the field is only coerced on load,
    # not on a freshly-created in-memory instance) — sync helpers call .prefixlen.
    obj.refresh_from_db()
    return obj


def make_server(name='DHCP Server 1', hostname='dhcp1.example.com', **kwargs):
    return DHCPServer.objects.create(name=name, hostname=hostname, **kwargs)


def make_failover(name='Failover 1', primary=None, secondary=None, **kwargs):
    primary = primary or make_server(name='Primary', hostname='primary.example.com')
    secondary = secondary or make_server(name='Secondary', hostname='secondary.example.com')
    return DHCPFailover.objects.create(
        name=name, primary_server=primary, secondary_server=secondary, **kwargs
    )


def make_scope(name='Scope 1', prefix=None, server=None, failover=None,
               start_ip='10.0.1.10', end_ip='10.0.1.254', **kwargs):
    prefix = prefix or make_prefix()
    if server is None and failover is None:
        # Distinct name so callers that also create a default make_server() in the
        # same test don't collide on the unique name.
        server = make_server(name='Scope Server', hostname='scope-server.example.com')
    return DHCPScope.objects.create(
        name=name, prefix=prefix, server=server, failover=failover,
        start_ip=start_ip, end_ip=end_ip, **kwargs
    )


# Codes 200–248 (plus 250, 251, 253, 254) are NOT seeded by migration 0002, so
# they are safe for fixtures that create option-code definitions without colliding
# with the built-in Windows DHCP options.
def make_option_definition(code=200, name='Test Option 200', **kwargs):
    return DHCPOptionCodeDefinition.objects.create(code=code, name=name, **kwargs)


def make_option_value(option_definition=None, value='10.0.0.1', **kwargs):
    option_definition = option_definition or make_option_definition()
    return DHCPOptionValue.objects.create(
        option_definition=option_definition, value=value, **kwargs
    )


# ---------------------------------------------------------------------------
# Fake PSU client + canned payloads (offline — no network)
# ---------------------------------------------------------------------------

class FakePSUClient:
    """
    Stand-in for ``api_client.PSUClient`` that returns pre-seeded data and records
    the writes it was asked to make. Construct with whatever the test needs:

        client = FakePSUClient(
            scopes=[...], leases={scope_id: [...]}, reservations={scope_id: [...]},
            exclusions={scope_id: [...]}, scope_options={scope_id: [...]},
            failover=[...],
        )

    Recorded writes are available on ``created_reservations``,
    ``created_exclusions``, ``deleted_exclusions``, ``created_scopes``,
    ``updated_scopes`` for assertions.
    """

    def __init__(self, scopes=None, leases=None, reservations=None,
                 exclusions=None, scope_options=None, failover=None,
                 health=None):
        self._scopes = scopes or []
        self._leases = leases or {}
        self._reservations = reservations or {}
        self._exclusions = exclusions or {}
        self._scope_options = scope_options or {}
        self._failover = failover or []
        self._health = health or {'version': '1.0.2'}

        self.created_reservations = []
        self.updated_reservations = []
        self.deleted_reservations = []
        self.created_exclusions = []
        self.deleted_exclusions = []
        self.created_scopes = []
        self.updated_scopes = []

    # --- reads ---
    def ping_read(self):
        return self._health

    def ping_write(self):
        return True

    def list_scopes(self, active_only=False):
        return list(self._scopes)

    def list_leases(self, scope_id=None):
        return list(self._leases.get(scope_id, []))

    def list_reservations(self, scope_id=None):
        return list(self._reservations.get(scope_id, []))

    def list_exclusions(self, scope_id):
        return list(self._exclusions.get(scope_id, []))

    def list_scope_options(self, scope_id):
        return list(self._scope_options.get(scope_id, []))

    def list_failover(self):
        return list(self._failover)

    # --- writes (recorded) ---
    def create_reservation(self, payload):
        self.created_reservations.append(payload)
        return payload

    def update_reservation(self, client_id, payload):
        self.updated_reservations.append((client_id, payload))
        return payload

    def delete_reservation(self, client_id):
        self.deleted_reservations.append(client_id)

    def create_exclusion(self, payload):
        self.created_exclusions.append(payload)
        return payload

    def delete_exclusion(self, payload):
        self.deleted_exclusions.append(payload)

    def create_scope(self, payload):
        self.created_scopes.append(payload)
        return payload

    def update_scope(self, scope_id, payload):
        self.updated_scopes.append((scope_id, payload))
        return payload


# Canned PSU response payloads (snake_case, the primary contract documented in
# api_client.py). PascalCase variants are produced inline by the dual-format test.
FAKE_SCOPE_SNAKE = {
    'scope_id': '10.0.1.0',
    'name': 'Building A',
    'start_ip': '10.0.1.10',
    'end_ip': '10.0.1.254',
    'subnet_mask': '255.255.255.0',
    'router': '10.0.1.1',
    'lease_duration_seconds': 86400,
}

FAKE_SCOPE_PASCAL = {
    'ScopeId': '10.0.1.0',
    'Name': 'Building A',
    'StartRange': '10.0.1.10',
    'EndRange': '10.0.1.254',
    'SubnetMask': '255.255.255.0',
    'Router': '10.0.1.1',
    'LeaseDuration': 86400,
}

FAKE_LEASE = {
    'ip_address': '10.0.1.50',
    'client_id': '00-11-22-33-44-55',
    'hostname': 'desktop-abc',
    'scope_id': '10.0.1.0',
    'lease_expiry': '2030-01-01T00:00:00Z',
    'address_state': 'Active',
}

FAKE_RESERVATION = {
    'ip_address': '10.0.1.100',
    'client_id': 'aa-bb-cc-dd-ee-ff',
    'name': 'printer-01',
    'description': '',
    'type': 'Dhcp',
}

FAKE_EXCLUSION = {
    'scope_id': '10.0.1.0',
    'start_ip': '10.0.1.200',
    'end_ip': '10.0.1.210',
}
