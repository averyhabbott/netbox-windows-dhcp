"""Shared utility helpers for netbox-windows-dhcp."""

import contextvars
from contextlib import contextmanager


def lease_lifetime_display(seconds: int) -> str:
    """
    Convert a lease lifetime in seconds to the most readable human string,
    preferring the largest clean (exact) unit.

    Examples:
        259200  -> "3 Days"
        86400   -> "1 Day"
        262800  -> "73 Hours"   (not an exact number of days)
        3600    -> "1 Hour"
        90      -> "1 Minute 30 Seconds"  (not exact minutes, fall through)
        90      -> actually 90s = 1.5 min -> "90 Seconds"
        60      -> "1 Minute"
        45      -> "45 Seconds"
    """
    if seconds <= 0:
        return f'{seconds} Seconds'

    days, rem = divmod(seconds, 86400)
    if rem == 0:
        return f'{days} {"Day" if days == 1 else "Days"}'

    hours, rem = divmod(seconds, 3600)
    if rem == 0:
        return f'{hours} {"Hour" if hours == 1 else "Hours"}'

    minutes, rem = divmod(seconds, 60)
    if rem == 0:
        return f'{minutes} {"Minute" if minutes == 1 else "Minutes"}'

    return f'{seconds} {"Second" if seconds == 1 else "Seconds"}'


def decompose_lease_lifetime(seconds: int) -> tuple[int, str]:
    """
    Split a lease lifetime in seconds into (value, unit) using the largest
    clean unit — the inverse of what lease_lifetime_display does.

    Returns a tuple of (value, unit) where unit is one of:
        'days', 'hours', 'minutes', 'seconds'
    """
    if seconds > 0:
        if seconds % 86400 == 0:
            return seconds // 86400, 'days'
        if seconds % 3600 == 0:
            return seconds // 3600, 'hours'
        if seconds % 60 == 0:
            return seconds // 60, 'minutes'
    return seconds, 'seconds'


# ---------------------------------------------------------------------------
# Client IDs
# ---------------------------------------------------------------------------

_CLIENT_ID_SEPARATORS = str.maketrans('', '', '-:. ')


def normalize_client_id(value) -> str:
    """
    Reduce a client ID to bare lowercase hex so different spellings compare equal:
    'AA:BB:CC:DD:EE:FF', 'aa-bb-cc-dd-ee-ff' and 'aabb.ccdd.eeff' → 'aabbccddeeff'.
    """
    if not value:
        return ''
    return str(value).translate(_CLIENT_ID_SEPARATORS).lower()


def format_client_id(value) -> str:
    """
    Return a client ID in the Windows DHCP style, 'aa-bb-cc-dd-ee-ff'. Values that
    aren't whole bytes of hex are returned normalized but otherwise unchanged.
    """
    normalized = normalize_client_id(value)
    if not normalized or len(normalized) % 2:
        return normalized
    try:
        int(normalized, 16)
    except ValueError:
        return normalized
    return '-'.join(normalized[i:i + 2] for i in range(0, len(normalized), 2))


# ---------------------------------------------------------------------------
# Scope membership
# ---------------------------------------------------------------------------

def _to_netaddr(ip):
    from netaddr import IPAddress as NetAddrIP
    try:
        return NetAddrIP(str(ip).split('/')[0])
    except Exception:
        return None


def in_scope_range(scope, ip) -> bool:
    """True if `ip` falls within the scope's start/end range (inclusive)."""
    addr = _to_netaddr(ip)
    start = _to_netaddr(scope.start_ip)
    end = _to_netaddr(scope.end_ip)
    if addr is None or start is None or end is None:
        return False
    return start <= addr <= end


def in_exclusion(scope, ip) -> bool:
    """True if `ip` falls within any of the scope's exclusion ranges."""
    addr = _to_netaddr(ip)
    if addr is None:
        return False
    for ex in scope.exclusion_ranges.all():
        start = _to_netaddr(ex.start_ip)
        end = _to_netaddr(ex.end_ip)
        if start is not None and end is not None and start <= addr <= end:
            return True
    return False


def scope_for_ip(ip, vrf_id):
    """
    Return the DHCPScope whose prefix contains `ip` in VRF `vrf_id` (None = global),
    or None. When several match, the most specific prefix wins, then a scope whose
    start/end range contains the IP.
    """
    from .models import DHCPScope

    addr = _to_netaddr(ip)
    if addr is None:
        return None
    candidates = list(
        DHCPScope.objects.filter(
            prefix__prefix__net_contains_or_equals=str(addr),
            prefix__vrf_id=vrf_id,
        ).select_related('prefix').prefetch_related('exclusion_ranges')
    )
    if not candidates:
        return None
    candidates.sort(key=lambda s: (
        -s.prefix.prefix.prefixlen,
        not in_scope_range(s, addr),
        s.pk,
    ))
    return candidates[0]


# ---------------------------------------------------------------------------
# Failover lookup
# ---------------------------------------------------------------------------

class AmbiguousFailover(Exception):
    """More than one failover with this name has the server as a partner."""


def find_failover(name, server):
    """
    The DHCPFailover called `name` that `server` is one of the two partners of, or None.
    Windows reports failovers by name only, and names are only unique per server — two
    separate server pairs can use the same one. Raises AmbiguousFailover when more than
    one matches (leftover data, say): the caller logs it and skips, never guesses.
    """
    from django.db.models import Q

    from .models import DHCPFailover

    if not name or server is None:
        return None
    matches = list(
        DHCPFailover.objects.filter(name=name)
        .filter(Q(primary_server=server) | Q(secondary_server=server))
        .select_related('primary_server', 'secondary_server')[:2]
    )
    if len(matches) > 1:
        raise AmbiguousFailover(
            f'More than one failover called {name!r} has {server} as a partner — '
            f'delete the leftover one'
        )
    return matches[0] if matches else None


def scopes_for_server(server):
    """
    The NetBox scopes tied to `server`: its standalone scopes, plus the scopes of every
    failover it's a partner in.
    """
    from django.db.models import Q

    from .models import DHCPScope

    if server is None:
        return DHCPScope.objects.none()
    return DHCPScope.objects.filter(
        Q(server=server)
        | Q(failover__primary_server=server)
        | Q(failover__secondary_server=server)
    )


# ---------------------------------------------------------------------------
# PSU script capabilities
# ---------------------------------------------------------------------------

def _version_tuple(version):
    try:
        return tuple(int(part) for part in str(version).strip().split('.'))
    except (TypeError, ValueError):
        return None


def _psu_at_least(server, min_version) -> bool:
    """True if the server's recorded PSU script version is at least `min_version`.
    A `min_version` of None, or an unknown or unparsable version, counts as not."""
    if not min_version:
        return False
    have = _version_tuple(getattr(server, 'psu_script_version', '') or '')
    need = _version_tuple(min_version)
    if not have or not need:
        return False
    return have >= need


def psu_supports_reservation_by_ip(server) -> bool:
    """
    True if the server's recorded PSU script version has the reservation
    update/delete-by-scope-and-IP endpoints (PSU_RESERVATION_BY_IP_MIN_VERSION).
    Unknown or unparsable versions count as unsupported.
    """
    from . import constants

    return _psu_at_least(server, constants.PSU_RESERVATION_BY_IP_MIN_VERSION)


def psu_supports_scope_state(server) -> bool:
    """
    True if the server's recorded PSU script version accepts a scope's `state` on
    create/update (PSU_SCOPE_STATE_MIN_VERSION). Unknown or unparsable versions count
    as unsupported.
    """
    from . import constants

    return _psu_at_least(server, constants.PSU_SCOPE_STATE_MIN_VERSION)


# ---------------------------------------------------------------------------
# Plugin-write flag
# ---------------------------------------------------------------------------

_plugin_write = contextvars.ContextVar('netbox_windows_dhcp_plugin_write', default=False)


def plugin_write_active() -> bool:
    """True while a plugin job is writing to NetBox (inside background_tasks._change_logging)."""
    return _plugin_write.get()


@contextmanager
def plugin_write():
    """Mark the enclosed block as a plugin job's own writes — see plugin_write_active()."""
    token = _plugin_write.set(True)
    try:
        yield
    finally:
        _plugin_write.reset(token)


# ---------------------------------------------------------------------------
# Maintenance mode
# ---------------------------------------------------------------------------

#: The maintenance fields maintenance_fields() sets.
MAINTENANCE_FIELDS = (
    'maintenance_mode', 'maintenance_notes', 'maintenance_enabled_at', 'maintenance_enabled_by',
)


def maintenance_fields(enabled: bool, notes: str, user) -> dict:
    """
    The maintenance field values for turning maintenance on or off. Turning it on records
    the notes, the time and the user; turning it off clears all three. Shared by the UI
    views and the API.
    """
    from django.utils import timezone

    if enabled:
        return {
            'maintenance_mode': True,
            'maintenance_notes': notes or '',
            'maintenance_enabled_at': timezone.now(),
            'maintenance_enabled_by': user,
        }
    return {
        'maintenance_mode': False,
        'maintenance_notes': '',
        'maintenance_enabled_at': None,
        'maintenance_enabled_by': None,
    }
