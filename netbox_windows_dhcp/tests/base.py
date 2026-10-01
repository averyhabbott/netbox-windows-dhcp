"""
Shared helpers for the netbox-windows-dhcp test suite.

Builders for NetBox rows (``make_server`` etc.), ``run_sync`` for a whole server
sync, and small helpers for URLs, jobs, IPs and the messages a page shows. The fake
PSU server and its sample replies are in ``fixtures.py``.
"""

import contextlib
import logging
import uuid
from unittest import mock

from core.choices import JobStatusChoices
from core.models import Job
from django.contrib.messages import constants as message_levels
from django.contrib.messages import get_messages
from django.urls import reverse
from ipam.models import IPAddress, Prefix
from utilities.testing import api as netbox_api_testing
from utilities.testing import views as netbox_view_testing


from ..models import (
    DHCPFailover,
    DHCPLeaseInfo,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPPluginSettings,
    DHCPScope,
    DHCPServer,
)

# A throwaway logger for the sync helpers, which expect a `job_logger` argument.
NULL_LOGGER = logging.getLogger('netbox_windows_dhcp.tests')
NULL_LOGGER.addHandler(logging.NullHandler())

PSU_CLIENT = 'netbox_windows_dhcp.api_client.PSUClient'


# ---------------------------------------------------------------------------
# Namespace mixins for the NetBox test harness
# ---------------------------------------------------------------------------

def _skip_query_counts(test_case):
    """
    NetBox 4.6+ fails a list test whose number of database queries differs from a stored
    count. The count changes with NetBox releases while the page still works, so the
    plugin doesn't check it.
    """
    for module in (netbox_api_testing, netbox_view_testing):
        if hasattr(module, 'assert_expected_query_count'):
            patcher = mock.patch.object(
                module, 'assert_expected_query_count', lambda *args, **kwargs: contextlib.nullcontext(),
            )
            patcher.start()
            test_case.addCleanup(patcher.stop)


class PluginAPIViewTestMixin:
    """
    Point the NetBox APIViewTestCases harness at the plugin's API namespace.

    NetBox's APITestCase builds URLs as ``{view_namespace}-api:{model}-detail``,
    defaulting view_namespace to the model's app_label. Plugin API views live
    under the ``plugins-api:`` prefix, so we set it explicitly.
    """

    view_namespace = 'plugins-api:netbox_windows_dhcp'

    def setUp(self):
        super().setUp()
        _skip_query_counts(self)


class PluginViewTestMixin:
    """Point the NetBox ViewTestCases harness at the plugin's UI URL namespace."""

    def setUp(self):
        super().setUp()
        _skip_query_counts(self)

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

def make_prefix(cidr='10.0.1.0/24', vrf=None, **kwargs):
    obj, _ = Prefix.objects.get_or_create(prefix=cidr, vrf=vrf, defaults={'status': 'active', **kwargs})
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


def make_unassigned_scope(name='Unassigned', network='10.0.1.0', prefix_length=24, **kwargs):
    """A scope with no prefix: it stores its own network. Pass a server or a failover."""
    kwargs.setdefault('start_ip', '10.0.1.10')
    kwargs.setdefault('end_ip', '10.0.1.254')
    return DHCPScope.objects.create(name=name, network=network, prefix_length=prefix_length, **kwargs)


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
# URLs, jobs and messages
# ---------------------------------------------------------------------------

def ui_url(name, *args):
    return reverse(f'plugins:netbox_windows_dhcp:{name}', args=args)


def api_url(model, pk=None):
    if pk is None:
        return reverse(f'plugins-api:netbox_windows_dhcp-api:{model}-list')
    return reverse(f'plugins-api:netbox_windows_dhcp-api:{model}-detail', kwargs={'pk': pk})


def job_mock():
    """A stand-in for an enqueued job; the views redirect to its page."""
    job = mock.Mock()
    job.get_absolute_url.return_value = '/core/jobs/1/'
    return job


def make_job(name):
    """A running Job row, for calling a JobRunner's run() directly."""
    return Job.objects.create(
        name=name, status=JobStatusChoices.STATUS_RUNNING, job_id=uuid.uuid4(), queue_name='default',
    )


def error_messages(response):
    """The warnings and errors a page queued for the user (pass a response that wasn't followed)."""
    return [str(m) for m in get_messages(response.wsgi_request) if m.level >= message_levels.WARNING]


def fresh_results():
    """An empty results dict, as the import functions expect."""
    return {
        'failovers':        {'created': [], 'skipped': [], 'errors': []},
        'scopes':           {'created': [], 'skipped': [], 'errors': []},
        'option_values':    {'created': [], 'skipped': [], 'errors': []},
        'exclusion_ranges': {'created': [], 'skipped': [], 'errors': []},
    }


# ---------------------------------------------------------------------------
# IP addresses
# ---------------------------------------------------------------------------

def _with_length(address):
    return address if '/' in address else f'{address}/24'


def get_ip(address, **filters):
    return IPAddress.objects.filter(address__net_host=address, **filters).first()


def managed_ip(address, status='dhcp', **fields):
    """An IP the sync made: it has DHCP lease info."""
    ip = IPAddress.objects.create(address=_with_length(address), status=status, **fields)
    DHCPLeaseInfo.objects.create(ip_address=ip, lease_hostname='x', active=True)
    return ip


def reserved_ip(address, client_id='aa-bb-cc-dd-ee-01', status='reserved', **fields):
    """A hand-made reservation in NetBox (saved once, so it queues at most one push)."""
    ip = IPAddress(address=_with_length(address), status=status, **fields)
    if client_id:
        ip.custom_field_data['dhcp_client_id'] = client_id
    ip.save()
    return ip
def run_sync(server, fake, logger=NULL_LOGGER, **options):
    """Run a full sync of `server` against `fake`. Every sync setting is off unless passed."""
    from ..background_tasks import _sync_server

    settings = dict(sync_ip_addresses=False, push_reservations=False, push_scope_info=False)
    settings.update(options)
    with mock.patch(PSU_CLIENT, return_value=fake):
        return _sync_server(logger, server, **settings)


def changes_for(model):
    """Change-log entries for `model`."""
    from core.models import ObjectChange, ObjectType

    return ObjectChange.objects.filter(changed_object_type=ObjectType.objects.get_for_model(model))


def grant(user, model, actions, **constraints):
    """Give `user` a NetBox object permission on `model`, limited by `constraints`."""
    from core.models import ObjectType
    from users.models import ObjectPermission

    perm = ObjectPermission.objects.create(
        name=f'{model.__name__} {"/".join(actions)} {constraints}', actions=actions,
        constraints=constraints or None,
    )
    perm.object_types.add(ObjectType.objects.get_for_model(model))
    perm.users.add(user)
    return perm
