"""
One-time import from a Windows DHCP server via PSU API.

Imports:
  1. Failover relationships  (matched to existing DHCPServer objects by hostname;
                              a new one copies the importing server's Default Scope VRF)
  2. Scopes                  (the prefix is looked for in the scope's default VRF — the
                              failover's, or the server's for a standalone scope — and
                              created there if missing and "Create Missing Prefixes on
                              Import" is on; otherwise the scope gets no prefix)
  3. Scope-level option values (DHCPOptionCodeDefinition created for unknown codes)
"""
import logging
from typing import Dict, Optional

from netaddr import AddrFormatError, IPNetwork

logger = logging.getLogger('netbox_windows_dhcp')


def run_import(server) -> Dict:
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

    # Failovers created in this run: their scopes are skipped until the user has checked
    # the failover's Default Scope VRF and taken it out of maintenance mode.
    new_failover_ids = set()
    for rf in remote_failovers:
        try:
            failover = _import_failover(rf, results, server=server)
            if failover is not None:
                new_failover_ids.add(failover.pk)
        except Exception as exc:
            name = rf.get('name') or rf.get('Name') or '(unknown)'
            results['failovers']['errors'].append(f'{name}: {exc}')
            from django.db import connection
            connection.close()  # reset a connection possibly corrupted by a mid-query job timeout

    # ------------------------------------------------------------------ #
    # 2. Scopes (+ scope-level option values)
    # ------------------------------------------------------------------ #
    try:
        remote_scopes = client.list_scopes(include_router=False)
    except PSUClientError as exc:
        results['scopes']['errors'].append(f'Could not fetch scope list: {exc}')
        remote_scopes = []

    for rs in remote_scopes:
        try:
            _import_scope(client, rs, results, server=server, new_failover_ids=new_failover_ids)
        except Exception as exc:
            scope_id = rs.get('scope_id') or rs.get('ScopeId') or '(unknown)'
            results['scopes']['errors'].append(f'{scope_id}: {exc}')
            from django.db import connection
            connection.close()  # reset a connection possibly corrupted by a mid-query job timeout

    return results


# ---------------------------------------------------------------------- #
# Failover helper
# ---------------------------------------------------------------------- #

NEW_FAILOVER_MAINTENANCE_NOTE = (
    'Created by Import from Server. Check its Default Scope VRF, take it out of '
    'maintenance mode, then re-run Import from Server to import its scopes.'
)


def _import_failover(rf: Dict, results: Dict, server=None):
    """
    Create the failover `rf` unless it's already in NetBox. "Already in NetBox" means a
    failover with this name that the importing `server` is a partner of — names are
    only unique per server. A new failover copies `server`'s Default Scope VRF and
    starts in maintenance mode. Returns the new failover, or None when none was created.
    """
    from .models import DHCPServer, DHCPFailover
    from .utils import AmbiguousFailover, find_failover

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

    def _skip_existing(partner) -> bool:
        """True (and recorded) if `partner` already has a failover with this name."""
        try:
            found = find_failover(name, partner)
        except AmbiguousFailover as exc:
            results['failovers']['errors'].append(f'{name}: {exc} — skipped.')
            return True
        if found is not None:
            results['failovers']['skipped'].append(f'{name} (already exists)')
            return True
        return False

    if server is not None and _skip_existing(server):
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

    if server is None and _skip_existing(primary):
        return

    # A new failover starts in maintenance mode, so its scopes wait until someone has
    # checked its Default Scope VRF (see run_import).
    from django.utils import timezone
    from .background_tasks import _service_user
    failover = DHCPFailover.objects.create(
        name=name,
        primary_server=primary,
        secondary_server=secondary,
        mode=mode,
        max_client_lead_time=mclt,
        max_response_delay=mrd,
        state_switchover_interval=ssi,
        enable_auth=enable_auth,
        default_scope_vrf_id=server.default_scope_vrf_id if server is not None else None,
        maintenance_mode=True,
        maintenance_enabled_at=timezone.now(),
        maintenance_enabled_by=_service_user(),
        maintenance_notes=NEW_FAILOVER_MAINTENANCE_NOTE,
    )
    results['failovers']['created'].append(name)
    results['failovers'].setdefault('maintenance', []).append(
        f'{name}: in maintenance mode, its scopes were not imported. {NEW_FAILOVER_MAINTENANCE_NOTE}'
    )
    return failover


# ---------------------------------------------------------------------- #
# Scope helper
# ---------------------------------------------------------------------- #

def _scope_prefix(cidr: str, vrf, create: bool):
    """
    (prefix, reason) for a learned scope with subnet `cidr`, looking only in `vrf`
    (None = global). One scope per prefix: when the prefix already belongs to another
    scope, or the VRF holds two prefixes with this network, the scope gets no prefix and
    `reason` says why. A missing prefix is created in `vrf` when `create` is on.
    """
    from ipam.models import Prefix

    from .models import DHCPScope

    vrf_label = f'VRF {vrf}' if vrf is not None else 'the global VRF'
    vrf_id = vrf.pk if vrf is not None else None
    found = list(Prefix.objects.filter(prefix=cidr, vrf_id=vrf_id)[:2])
    if len(found) > 1:
        return None, f'more than one prefix {cidr} exists in {vrf_label}'
    if found:
        owner = DHCPScope.objects.filter(prefix=found[0]).first()
        if owner is not None:
            return None, f'prefix {cidr} in {vrf_label} already belongs to scope "{owner.name}"'
        return found[0], ''
    if create:
        return Prefix.objects.create(prefix=cidr, vrf_id=vrf_id, status='active'), ''
    return None, f'no prefix {cidr} in {vrf_label}, and "Create Missing Prefixes on Import" is off'


def _import_scope(client, rs: Dict, results: Dict, server=None, new_failover_ids=frozenset()):
    """
    Create the NetBox scope for server scope `rs`, unless `server` already has one with
    this network. Returns the new or existing DHCPScope, or None when skipped.

    Skipped with an error: a scope in a failover NetBox doesn't have (it's never imported
    as a standalone scope). Skipped: a scope in a failover created in this run
    (`new_failover_ids`), which waits until its Default Scope VRF has been checked.
    """
    from .models import DHCPPluginSettings, DHCPScope
    from .utils import AmbiguousFailover, find_failover, scopes_for_server

    scope_id    = rs.get('scope_id')    or rs.get('ScopeId')    or ''
    name        = rs.get('name')        or rs.get('Name')        or scope_id
    start_ip    = rs.get('start_ip')    or rs.get('StartRange')  or ''
    end_ip      = rs.get('end_ip')      or rs.get('EndRange')    or ''
    subnet_mask = rs.get('subnet_mask') or rs.get('SubnetMask')  or ''
    router_raw  = rs.get('router')      or rs.get('Router')      or ''
    lease_secs  = int(rs.get('lease_duration_seconds') or rs.get('LeaseDuration') or 86400)
    description = (rs.get('description') or rs.get('Description') or '')[:200]
    state       = rs.get('state')       or rs.get('State')
    active      = state is None or str(state).strip().lower() == 'active'

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

    net = IPNetwork(cidr)
    network = str(net.network)

    # The failover the server says the scope is in — by name and partner server,
    # since failover names are only unique per server.
    failover = None
    failover_name = rs.get('failover_name') or rs.get('FailoverName') or rs.get('FailoverRelationshipName') or ''
    if failover_name:
        try:
            failover = find_failover(failover_name, server)
        except AmbiguousFailover as exc:
            results['scopes']['errors'].append(f'{scope_id}: {exc} — skipped.')
            return None
        if failover is None:
            results['scopes']['errors'].append(
                f'{scope_id}: the server reports failover {failover_name!r}, which isn\'t in NetBox '
                f'— skipped. Re-run Import from Server (both of the failover\'s servers must be '
                f'in NetBox first).'
            )
            return None
        if failover.pk in new_failover_ids:
            results['scopes']['skipped'].append(
                f'{name} ({cidr}): failover {failover_name!r} was just created and is in maintenance mode'
            )
            return None

    # Already in NetBox? Match on this server + network, not the name, so a scope renamed
    # on Windows isn't imported a second time. Exclusion ranges are still imported, in
    # case they were added after the scope was first imported.
    matches = list(scopes_for_server(server).filter(network=network)[:2])
    if len(matches) > 1:
        results['scopes']['errors'].append(
            f'{scope_id}: more than one NetBox scope uses network {network} on {server} — skipped.'
        )
        return None
    if matches:
        existing = matches[0]
        results['scopes']['skipped'].append(f'{existing.name} ({cidr})')
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

    # The prefix comes from the failover's default VRF, or the server's for a standalone
    # scope (blank = global).
    if failover is not None:
        vrf = failover.default_scope_vrf
    else:
        vrf = server.default_scope_vrf if server is not None else None
    # Read the scope's options before creating it: newer PSU scripts leave the router
    # out of the scope list, so it comes from Option 3 here. If they can't be read and
    # the scope list didn't carry the router, skip the scope rather than import it
    # with its router missing.
    from .api_client import PSUClientError
    try:
        remote_opts = client.list_scope_options(scope_id)
        opts_error = None
    except PSUClientError as exc:
        remote_opts = []
        opts_error = exc
    if not remote_has_router(rs):
        if opts_error is not None:
            results['scopes']['errors'].append(
                f'{scope_id}: could not fetch options (needed for the router): {opts_error} '
                f'— skipped. Re-run Import from Server.'
            )
            return None
        router = router_from_options(remote_opts)

    prefix_obj, no_prefix_reason = _scope_prefix(
        cidr, vrf, create=DHCPPluginSettings.load().create_missing_prefixes,
    )

    scope = DHCPScope.objects.create(
        name=name,
        description=description,
        prefix=prefix_obj,
        network=network,
        prefix_length=net.prefixlen,
        start_ip=start_ip,
        end_ip=end_ip,
        router=router,
        lease_lifetime=lease_secs,
        active=active,
        failover=failover,
        server=server if failover is None else None,
    )
    label = f'{name} ({cidr})'
    results['scopes']['created'].append(label)
    if no_prefix_reason:
        # Lands on the Unassigned Scopes list.
        results['scopes'].setdefault('unassigned', []).append(f'{label}: no prefix — {no_prefix_reason}')

    # Import scope-level option values
    if opts_error is None:
        for ro in remote_opts:
            _import_option_value(scope, ro, results)
    else:
        results['option_values']['errors'].append(
            f'Scope {name}: could not fetch options: {opts_error}'
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


def router_from_options(options) -> Optional[str]:
    """
    The router IP (Option 3, no vendor class) from a scope's raw PSU option list,
    or None when the scope has no router. With several routers, the first one.
    """
    for ro in options:
        parsed = normalize_option_value(ro)
        if parsed is None:
            continue
        code, value, _name, vendor_class = parsed
        if code == 3 and not vendor_class:
            first = value.split(', ')[0].strip()
            return first if first not in ('', '0.0.0.0') else None
    return None


def remote_has_router(remote: Dict) -> bool:
    """True if a scope-list record carries the router itself (older PSU scripts, or
    include_router left on); False means it has to come from the scope's options."""
    return 'router' in remote or 'Router' in remote


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
