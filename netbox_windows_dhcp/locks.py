"""
Write locks: stop users making IP, range and exclusion changes the next sync would
undo or delete.

Inside a scope's dynamic range (start/end, minus exclusions) the sync keeps only the IPs
the rules allow there — see _sync_scope_ips(). These checks refuse everything else up
front. They're only active while "Sync IP Addresses" is on (with it off, the sync never
cleans up IPs), and never apply to the plugin's own writes: the sync doesn't call
full_clean(), and plugin_write_active() is checked as well.

An IP is protected — and exempt from every lock — when it carries the sync-protect tag,
or sits inside a prefix that carries it.
"""

from netaddr import IPAddress as NetAddrIP

# How many offending IPs an error message lists before summarising the rest.
_MAX_LISTED = 5


def _addr(value):
    try:
        return NetAddrIP(str(value).split('/')[0])
    except Exception:
        return None


def _locks_settings():
    """The plugin settings if the locks are active, else None."""
    from .models import DHCPPluginSettings
    from .utils import plugin_write_active

    if plugin_write_active():
        return None
    cfg = DHCPPluginSettings.load()
    return cfg if cfg.sync_ip_addresses else None


class Coverage:
    """A scope's dynamic range: start/end, minus exclusions. Empty when unusable."""

    def __init__(self, start=None, end=None, exclusions=()):
        self.start = _addr(start) if start else None
        self.end = _addr(end) if end else None
        self.exclusions = []
        for ex_start, ex_end in exclusions:
            s, e = _addr(ex_start), _addr(ex_end)
            if s is not None and e is not None:
                self.exclusions.append((s, e))

    @classmethod
    def of_scope(cls, scope, exclusions=None):
        """`scope`'s stored range; `exclusions` defaults to its stored exclusions."""
        if exclusions is None:
            exclusions = [(ex.start_ip, ex.end_ip) for ex in scope.exclusion_ranges.all()]
        return cls(scope.start_ip, scope.end_ip, exclusions)

    def __contains__(self, addr):
        if addr is None or self.start is None or self.end is None:
            return False
        if not self.start <= addr <= self.end:
            return False
        return not any(s <= addr <= e for s, e in self.exclusions)


def _protected_networks(cfg):
    from .background_tasks import _protected_prefix_networks
    return _protected_prefix_networks(cfg)


def _in_networks(addr, networks) -> bool:
    import ipaddress as _ipmod

    if addr is None or not networks:
        return False
    try:
        ip = _ipmod.ip_address(str(addr))
    except ValueError:
        return False
    return any(ip in net for net in networks)


def _tag_slugs(tags):
    slugs = set()
    for tag in tags or ():
        slug = getattr(tag, 'slug', None)
        if slug is None and isinstance(tag, dict):
            slug = tag.get('slug')
        slugs.add(slug if slug is not None else str(tag))
    return slugs


def _pending_tag_slugs(instance):
    """
    The tags an IP will have once saved. Forms and the API hand them over in
    instance._m2m_values before validating; otherwise (bulk edit adds tags only after
    saving) they're the IP's current tags.
    """
    m2m = getattr(instance, '_m2m_values', None) or {}
    if 'tags' in m2m:
        return _tag_slugs(m2m['tags'])
    if instance.pk:
        return set(instance.tags.values_list('slug', flat=True))
    return set()


def _dynamic_scopes(addr, vrf_id):
    """Every scope in VRF `vrf_id` whose dynamic range contains `addr`."""
    from .models import DHCPScope

    scopes = DHCPScope.objects.filter(
        prefix__prefix__net_contains_or_equals=str(addr), prefix__vrf_id=vrf_id,
    ).select_related('prefix').prefetch_related('exclusion_ranges')
    return [scope for scope in scopes if addr in Coverage.of_scope(scope)]


def _allowed_statuses_text(cfg):
    if cfg.push_reservations:
        return (
            f'only IPs with status "{cfg.reservation_status}" (pushed to the server as '
            f'reservations) and IPs the DHCP server manages can exist there'
        )
    return 'only IPs the DHCP server hands out or reserves can exist there'


# ---------------------------------------------------------------------------
# IP lock
# ---------------------------------------------------------------------------

def check_ip_address(instance):
    """
    Refuse creating or editing an IP inside a scope's dynamic range unless the rules
    allow it there. Raises ValidationError.
    """
    from django.core.exceptions import ValidationError

    cfg = _locks_settings()
    if cfg is None:
        return
    addr = _addr(instance.address)
    if addr is None:
        return

    if cfg.push_reservations and instance.status == cfg.reservation_status:
        return

    scopes = _dynamic_scopes(addr, instance.vrf_id)
    if not scopes:
        return

    if cfg.sync_protect_tag_id and cfg.sync_protect_tag.slug in _pending_tag_slugs(instance):
        return
    if _in_networks(addr, _protected_networks(cfg)):
        return
    if _is_managed_unmoved(instance, addr, cfg):
        return

    scope = scopes[0]
    hint = ''
    if cfg.sync_protect_tag_id:
        hint = f' Tag it "{cfg.sync_protect_tag.name}" to keep it anyway, or use an address outside the range.'
    raise ValidationError(
        f'{addr} is inside the DHCP range of scope "{scope.name}" '
        f'({scope.start_ip}–{scope.end_ip}): {_allowed_statuses_text(cfg)}. '
        f'The next sync would delete this IP.{hint}'
    )


def _is_managed_unmoved(instance, addr, cfg) -> bool:
    """An existing DHCP-managed IP (has lease info), keeping its address, VRF and a DHCP status."""
    from ipam.models import IPAddress

    from .models import DHCPLeaseInfo

    if not instance.pk:
        return False
    if instance.status not in (cfg.lease_status, cfg.reservation_status):
        return False
    stored = IPAddress.objects.filter(pk=instance.pk).values('address', 'vrf_id').first()
    if not stored or _addr(stored['address']) != addr or stored['vrf_id'] != instance.vrf_id:
        return False
    return DHCPLeaseInfo.objects.filter(ip_address_id=instance.pk).exists()


# ---------------------------------------------------------------------------
# Range and exclusion guard
# ---------------------------------------------------------------------------

def _newly_disallowed(prefix, before, after, cfg):
    """
    Addresses of IPs in `prefix` (and its VRF) that `after` covers, `before` didn't, and
    the rules don't allow inside a dynamic range.
    """
    from ipam.models import IPAddress

    from .models import DHCPLeaseInfo

    protect_slug = cfg.sync_protect_tag.slug if cfg.sync_protect_tag_id else ''
    networks = None
    candidates = []
    for ip in IPAddress.objects.filter(
        address__net_contained_or_equal=str(prefix.prefix), vrf_id=prefix.vrf_id,
    ).prefetch_related('tags'):
        addr = _addr(ip.address)
        if addr not in after or addr in before:
            continue
        if cfg.push_reservations and ip.status == cfg.reservation_status:
            continue
        if protect_slug and protect_slug in ip.tags.slugs():
            continue
        if networks is None:
            networks = _protected_networks(cfg)
        if _in_networks(addr, networks):
            continue
        candidates.append((addr, ip))
    if not candidates:
        return []

    managed = set(DHCPLeaseInfo.objects.filter(
        ip_address_id__in=[ip.pk for _, ip in candidates],
    ).values_list('ip_address_id', flat=True))
    dhcp_statuses = (cfg.lease_status, cfg.reservation_status)
    return sorted(
        addr for addr, ip in candidates
        if not (ip.pk in managed and ip.status in dhcp_statuses)
    )


def _covered_error(what, addrs, cfg):
    listed = ', '.join(str(a) for a in addrs[:_MAX_LISTED])
    if len(addrs) > _MAX_LISTED:
        listed += f' and {len(addrs) - _MAX_LISTED} more'
    return (
        f'{what} would put {len(addrs)} existing IP(s) inside the DHCP range ({listed}), '
        f'but {_allowed_statuses_text(cfg)} — the next sync would delete them. Move, '
        f'delete or re-status those IPs first'
        + (f', or tag them "{cfg.sync_protect_tag.name}"' if cfg.sync_protect_tag_id else '')
        + '.'
    )


def check_scope_range(scope):
    """Refuse a scope save whose new range would cover IPs not allowed there. Raises ValidationError."""
    from django.core.exceptions import ValidationError

    from .models import DHCPScope

    cfg = _locks_settings()
    if cfg is None or not scope.prefix_id:
        return

    stored = None
    if scope.pk:
        stored = DHCPScope.objects.filter(pk=scope.pk).prefetch_related('exclusion_ranges').first()
    exclusions = [(ex.start_ip, ex.end_ip) for ex in stored.exclusion_ranges.all()] if stored else []
    after = Coverage(scope.start_ip, scope.end_ip, exclusions)
    before = Coverage.of_scope(stored) if stored and stored.prefix_id == scope.prefix_id else Coverage()

    addrs = _newly_disallowed(scope.prefix, before, after, cfg)
    if addrs:
        raise ValidationError(_covered_error('This range', addrs, cfg))


def _exclusions_except(scope, pk):
    return [(ex.start_ip, ex.end_ip) for ex in scope.exclusion_ranges.all() if ex.pk != pk]


def check_exclusion_change(exclusion):
    """
    Refuse an exclusion save that would put IPs not allowed there back into a dynamic
    range (shrinking or moving an exclusion, or moving it to another scope).
    Raises ValidationError.
    """
    from django.core.exceptions import ValidationError

    from .models import DHCPExclusionRange

    cfg = _locks_settings()
    if cfg is None or not exclusion.pk:
        return  # a new exclusion only ever shrinks the range
    stored = DHCPExclusionRange.objects.filter(pk=exclusion.pk).select_related('scope__prefix').first()
    if stored is None:
        return

    old_scope = stored.scope
    if not old_scope.prefix_id:
        return  # no prefix, so no NetBox IPs the change could expose
    others = _exclusions_except(old_scope, exclusion.pk)
    before = Coverage.of_scope(old_scope)
    if exclusion.scope_id == stored.scope_id:
        after = Coverage.of_scope(old_scope, others + [(exclusion.start_ip, exclusion.end_ip)])
    else:
        after = Coverage.of_scope(old_scope, others)

    addrs = _newly_disallowed(old_scope.prefix, before, after, cfg)
    if addrs:
        raise ValidationError(_covered_error('This change', addrs, cfg))


def check_exclusion_delete(exclusion):
    """Refuse deleting an exclusion whose IPs aren't allowed in the dynamic range. Raises AbortRequest."""
    from utilities.exceptions import AbortRequest

    cfg = _locks_settings()
    if cfg is None or not exclusion.scope_id:
        return
    scope = exclusion.scope
    if not scope.prefix_id:
        return
    before = Coverage.of_scope(scope)
    after = Coverage.of_scope(scope, _exclusions_except(scope, exclusion.pk))

    addrs = _newly_disallowed(scope.prefix, before, after, cfg)
    if addrs:
        raise AbortRequest(_covered_error(f'Deleting exclusion {exclusion}', addrs, cfg))


# ---------------------------------------------------------------------------
# push_scope_info off: what can still change
# ---------------------------------------------------------------------------
# The DHCP server owns scope settings then, so creating scopes, exclusions and option
# values is refused, and an edit may only change the NetBox-only fields below (the UI
# shows the rest read-only). Deletes are allowed: they never reach the server, and the
# next sync imports again anything still on it.

#: Scope fields an edit may change while push_scope_info is off.
SCOPE_EDITABLE_WHEN_PUSH_OFF = ('prefix', 'maintenance_mode', 'maintenance_notes', 'tags', 'custom_fields')
#: Exclusion range fields an edit may change while push_scope_info is off.
EXCLUSION_EDITABLE_WHEN_PUSH_OFF = ('description', 'tags', 'custom_fields')


def server_owns_scope_info() -> bool:
    """True while push_scope_info is off and this isn't a plugin job's own write."""
    from .models import DHCPPluginSettings
    from .utils import plugin_write_active

    return not plugin_write_active() and not DHCPPluginSettings.load().push_scope_info


# How many scopes the option-value delete guard names before summarising the rest.
_MAX_SCOPES_LISTED = 3


def check_option_value_delete(option_value):
    """
    Refuse deleting an option value any scope still uses (in both push modes): with
    push_scope_info on, the next push would strip it from every one of those scopes on
    the server. Raises AbortRequest.
    """
    from django.utils.html import escape
    from utilities.exceptions import AbortRequest

    scopes = list(option_value.scopes.order_by('name').values_list('name', flat=True))
    if not scopes:
        return
    listed = ', '.join(f'"{escape(name)}"' for name in scopes[:_MAX_SCOPES_LISTED])
    if len(scopes) > _MAX_SCOPES_LISTED:
        listed += f' and {len(scopes) - _MAX_SCOPES_LISTED} more'
    raise AbortRequest(
        f'Option value "{escape(option_value)}" is used by {len(scopes)} scope(s) ({listed}). '
        f'Remove it from those scopes first, then delete it.'
    )


def check_option_codes(option_values):
    """
    Refuse a set of option values that has two values for the same option code — a scope
    can hold only one value per code. Used by the scope form, the API and bulk edit.
    Raises ValidationError.
    """
    from django.core.exceptions import ValidationError

    seen = set()
    duplicates = set()
    for ov in option_values:
        code = ov.option_definition.code
        if code in seen:
            duplicates.add(code)
        seen.add(code)
    if duplicates:
        codes = ', '.join(str(c) for c in sorted(duplicates))
        raise ValidationError(
            f'A scope cannot have more than one value for the same option code. '
            f'Duplicate code(s): {codes}.'
        )
