"""
One-time import from a Windows DHCP server via PSU API.

Imports:
  1. Failover relationships  (matched to existing DHCPServer objects by hostname)
  2. Scopes                  (NetBox Prefix is created if it doesn't exist)
  3. Scope-level option values (DHCPOptionCodeDefinition created for unknown codes)
"""
import logging
from typing import Dict

from netaddr import AddrFormatError, IPNetwork

logger = logging.getLogger('netbox_windows_dhcp')


def run_import(server, active_only: bool = False) -> Dict:
    """
    Connect to *server* and import failovers, scopes, and scope options.
    Returns a results dict suitable for template rendering.
    """
    from .api_client import PSUClient, PSUClientError

    results = {
        'failovers':        {'created': [], 'skipped': [], 'errors': []},
        'scopes':           {'created': [], 'skipped': [], 'errors': []},
        'option_values':    {'created': [], 'skipped': [], 'errors': []},
        'exclusion_ranges': {'created': [], 'skipped': [], 'errors': []},
    }

    client = PSUClient(server)

    # ------------------------------------------------------------------ #
    # 1. Failover relationships
    # ------------------------------------------------------------------ #
    try:
        remote_failovers = client.list_failover()
    except PSUClientError as exc:
        results['failovers']['errors'].append(f'Could not fetch failover list: {exc}')
        remote_failovers = []

    for rf in remote_failovers:
        try:
            _import_failover(rf, results)
        except Exception as exc:
            name = rf.get('name') or rf.get('Name') or '(unknown)'
            results['failovers']['errors'].append(f'{name}: {exc}')

    # ------------------------------------------------------------------ #
    # 2. Scopes (+ scope-level option values)
    # ------------------------------------------------------------------ #
    try:
        remote_scopes = client.list_scopes(active_only=active_only)
    except PSUClientError as exc:
        results['scopes']['errors'].append(f'Could not fetch scope list: {exc}')
        remote_scopes = []

    for rs in remote_scopes:
        try:
            _import_scope(client, rs, results, server=server)
        except Exception as exc:
            scope_id = rs.get('scope_id') or rs.get('ScopeId') or '(unknown)'
            results['scopes']['errors'].append(f'{scope_id}: {exc}')

    return results


# ---------------------------------------------------------------------- #
# Failover helper
# ---------------------------------------------------------------------- #

def _import_failover(rf: Dict, results: Dict):
    from .models import DHCPServer, DHCPFailover

    name               = rf.get('name')               or rf.get('Name')              or ''
    primary_hostname   = rf.get('primary_server')     or rf.get('PrimaryServer')     or ''
    secondary_hostname = rf.get('secondary_server')   or rf.get('SecondaryServer')   or ''
    mode               = rf.get('mode')               or rf.get('Mode')              or 'LoadBalance'
    mclt               = int(rf.get('max_client_lead_time')      or rf.get('MaxClientLeadTime')      or 3600)
    mrd                = int(rf.get('max_response_delay')        or rf.get('MaxResponseDelay')       or 30)
    ssi_raw            = rf.get('state_switchover_interval')     or rf.get('AutoStateTransitionInterval')
    ssi                = int(ssi_raw) if ssi_raw else None
    enable_auth        = bool(rf.get('enable_auth') or rf.get('EnableAuth') or False)

    if not name:
        results['failovers']['errors'].append('Failover record has no name — skipped.')
        return

    if DHCPFailover.objects.filter(name=name).exists():
        results['failovers']['skipped'].append(f'{name} (already exists)')
        return

    # Resolve primary server
    try:
        primary = DHCPServer.objects.get(hostname=primary_hostname)
    except DHCPServer.DoesNotExist:
        results['failovers']['errors'].append(
            f'{name}: primary server "{primary_hostname}" not found — '
            f'add it as a DHCP Server in NetBox first.'
        )
        return
    except DHCPServer.MultipleObjectsReturned:
        primary = DHCPServer.objects.filter(hostname=primary_hostname).first()

    # Resolve secondary server
    try:
        secondary = DHCPServer.objects.get(hostname=secondary_hostname)
    except DHCPServer.DoesNotExist:
        results['failovers']['errors'].append(
            f'{name}: secondary server "{secondary_hostname}" not found — '
            f'add it as a DHCP Server in NetBox first.'
        )
        return
    except DHCPServer.MultipleObjectsReturned:
        secondary = DHCPServer.objects.filter(hostname=secondary_hostname).first()

    DHCPFailover.objects.create(
        name=name,
        primary_server=primary,
        secondary_server=secondary,
        mode=mode,
        max_client_lead_time=mclt,
        max_response_delay=mrd,
        state_switchover_interval=ssi,
        enable_auth=enable_auth,
    )
    results['failovers']['created'].append(name)


# ---------------------------------------------------------------------- #
# Scope helper
# ---------------------------------------------------------------------- #

def _import_scope(client, rs: Dict, results: Dict, server=None):
    from .models import DHCPFailover, DHCPScope
    from ipam.models import Prefix

    scope_id    = rs.get('scope_id')    or rs.get('ScopeId')    or ''
    name        = rs.get('name')        or rs.get('Name')        or scope_id
    start_ip    = rs.get('start_ip')    or rs.get('StartRange')  or ''
    end_ip      = rs.get('end_ip')      or rs.get('EndRange')    or ''
    subnet_mask = rs.get('subnet_mask') or rs.get('SubnetMask')  or ''
    router_raw  = rs.get('router')      or rs.get('Router')      or ''
    lease_secs  = int(rs.get('lease_duration_seconds') or rs.get('LeaseDuration') or 86400)

    router = router_raw if router_raw not in ('', '0.0.0.0') else None

    if not scope_id:
        results['scopes']['errors'].append('Scope record missing scope_id — skipped.')
        return None

    # Build CIDR prefix string
    try:
        cidr = str(IPNetwork(f'{scope_id}/{subnet_mask}').cidr)
    except (AddrFormatError, Exception) as exc:
        results['scopes']['errors'].append(
            f'{scope_id}: cannot compute CIDR '
            f'(scope_id={scope_id!r}, subnet_mask={subnet_mask!r}): {exc}'
        )
        return None

    # Find or create the NetBox Prefix
    from .models import DHCPPluginSettings
    if DHCPPluginSettings.load().create_missing_prefixes:
        prefix_obj, _ = Prefix.objects.get_or_create(
            prefix=cidr,
            defaults={'status': 'active'},
        )
    else:
        try:
            prefix_obj = Prefix.objects.get(prefix=cidr)
        except Prefix.DoesNotExist:
            results['scopes']['errors'].append(
                f'{scope_id}: prefix {cidr} does not exist in NetBox and '
                f'"Create Missing Prefixes on Import" is disabled — skipped.'
            )
            return None

    # Skip if a scope with this name + prefix already exists, but still import
    # exclusion ranges in case they were added after the scope was first imported.
    existing = DHCPScope.objects.filter(prefix=prefix_obj, name=name).first()
    if existing:
        results['scopes']['skipped'].append(f'{name} ({cidr})')
        from .api_client import PSUClientError
        try:
            remote_exclusions = client.list_exclusions(scope_id)
            for re in remote_exclusions:
                _import_exclusion_range(existing, re, results)
        except PSUClientError as exc:
            results['exclusion_ranges']['errors'].append(
                f'Scope {name}: could not fetch exclusion ranges: {exc}'
            )
        return existing

    # Optionally link a failover if the API tells us the failover name
    failover = None
    failover_name = rs.get('failover_name') or rs.get('FailoverName') or rs.get('FailoverRelationshipName') or ''
    if failover_name:
        failover = DHCPFailover.objects.filter(name=failover_name).first()

    scope = DHCPScope.objects.create(
        name=name,
        prefix=prefix_obj,
        start_ip=start_ip,
        end_ip=end_ip,
        router=router,
        lease_lifetime=lease_secs,
        failover=failover,
        server=server if failover is None else None,
    )
    results['scopes']['created'].append(f'{name} ({cidr})')

    # Import scope-level option values
    from .api_client import PSUClientError
    try:
        remote_opts = client.list_scope_options(scope_id)
        for ro in remote_opts:
            _import_option_value(scope, ro, results)
    except PSUClientError as exc:
        results['option_values']['errors'].append(
            f'Scope {name}: could not fetch options: {exc}'
        )

    # Import exclusion ranges
    try:
        remote_exclusions = client.list_exclusions(scope_id)
        for re in remote_exclusions:
            _import_exclusion_range(scope, re, results)
    except PSUClientError as exc:
        results['exclusion_ranges']['errors'].append(
            f'Scope {name}: could not fetch exclusion ranges: {exc}'
        )

    return scope


# ---------------------------------------------------------------------- #
# Option value helper
# ---------------------------------------------------------------------- #

#: Option 3 (Router) and Option 51 (Lease Time) are stored on DHCPScope's own
#: router/lease_lifetime fields instead of as DHCPOptionValue rows, so every
#: option-value code path (import, and the recurring pull/push sync) skips them.
SCOPE_FIELD_OPTION_CODES = (3, 51)


def normalize_option_value(ro: Dict):
    """
    Parse a raw PSU scope-option dict into (code, value, name, vendor_class).

    Returns None if no option code is present. `value` is always a string —
    PSU returns multi-value options (e.g. DNS servers) as a JSON array, which
    is joined into a comma-separated string for storage in DHCPOptionValue.value.
    """
    # PSU returns 'code'; older/alternative shapes use 'OptionId' or 'option_id'
    code_raw = ro.get('code') or ro.get('OptionId') or ro.get('option_id')
    if code_raw is None:
        return None

    code = int(code_raw)
    value_raw    = ro.get('value') or ro.get('Value') or ''
    opt_name     = ro.get('name')  or ro.get('Name')  or ''
    vendor_class = ro.get('vendor_class') or ro.get('VendorClass') or ''

    if isinstance(value_raw, list):
        value = ', '.join(str(v) for v in value_raw if v is not None)
    else:
        value = str(value_raw)

    return code, value, opt_name, vendor_class


def denormalize_option_value(value: str):
    """
    Reverse of the comma-join in normalize_option_value(), for sending a value
    back to PSU as the array Set-DhcpServerv4OptionValue expects.

    Note: this is a best-effort split on the exact ', ' separator used above —
    a value containing a literal ', ' inside a single item won't round-trip.
    """
    return value.split(', ') if value else []


def get_or_create_option_value(code: int, value: str, name: str = '', vendor_class: str = ''):
    """
    Find-or-create the DHCPOptionCodeDefinition and DHCPOptionValue for a
    normalized (code, value) pair. Shared by the one-time import and the
    recurring pull-direction sync so both create identical records.
    """
    from .models import DHCPOptionCodeDefinition, DHCPOptionValue

    # Find or create the option code definition, using the PSU-provided name
    # when creating a new record; never overwrite the name on an existing record.
    opt_def, _ = DHCPOptionCodeDefinition.objects.get_or_create(
        code=code,
        defaults={
            'name': name or f'Option {code}',
            'data_type': 'String',
            'is_builtin': False,
            'vendor_class': vendor_class,
        },
    )

    opt_val, created = DHCPOptionValue.objects.get_or_create(
        option_definition=opt_def,
        value=value,
        defaults={'friendly_name': ''},
    )
    return opt_val, created


def _import_option_value(scope, ro: Dict, results: Dict):
    parsed = normalize_option_value(ro)
    if parsed is None:
        return

    code, value, opt_name, vendor_class = parsed
    if code in SCOPE_FIELD_OPTION_CODES:
        return

    opt_val, created = get_or_create_option_value(code, value, opt_name, vendor_class)
    scope.option_values.add(opt_val)

    label = f'Option {code} ({opt_val.option_definition.name}): {value} — scope: {scope.name}'
    if created:
        results['option_values']['created'].append(label)
    else:
        results['option_values']['skipped'].append(label)


# ---------------------------------------------------------------------- #
# Exclusion range helper
# ---------------------------------------------------------------------- #

def _import_exclusion_range(scope, re: Dict, results: Dict):
    from .models import DHCPExclusionRange

    start_ip = re.get('start_ip') or re.get('StartRange') or ''
    end_ip   = re.get('end_ip')   or re.get('EndRange')   or ''

    if not start_ip or not end_ip:
        results['exclusion_ranges']['errors'].append(
            f'Scope {scope.name}: exclusion range missing start_ip or end_ip — skipped.'
        )
        return

    _, created = DHCPExclusionRange.objects.get_or_create(
        scope=scope,
        start_ip=start_ip,
        end_ip=end_ip,
    )

    label = f'{start_ip} – {end_ip} (scope: {scope.name})'
    if created:
        results['exclusion_ranges']['created'].append(label)
    else:
        results['exclusion_ranges']['skipped'].append(label)
