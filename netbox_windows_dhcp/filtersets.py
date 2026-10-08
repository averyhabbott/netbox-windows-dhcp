import django_filters
import netaddr
from django.db.models import Q
from netbox.filtersets import BaseFilterSet, NetBoxModelFilterSet
from utilities.filtersets import register_filterset
from utilities.filters import MultiValueCharFilter

from .choices import DHCPServerAccessChoices, DHCPServerHealthChoices
from .models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPLeaseInfo,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)

# Callable queryset factories for filters that reference models from other apps.
# Using callables avoids the need to set querysets in __init__.
def _site_qs(request=None):
    from dcim.models import Site
    return Site.objects.all()

def _location_qs(request=None):
    from dcim.models import Location
    return Location.objects.all()

def _tenant_qs(request=None):
    from tenancy.models import Tenant
    return Tenant.objects.all()

def _vrf_qs(request=None):
    from ipam.models import VRF
    return VRF.objects.all()


@register_filterset
class DHCPServerFilterSet(NetBoxModelFilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    hostname = django_filters.CharFilter(lookup_expr='icontains')
    health_status = django_filters.MultipleChoiceFilter(choices=DHCPServerHealthChoices)
    access_level = django_filters.MultipleChoiceFilter(choices=DHCPServerAccessChoices)
    default_scope_vrf_id = django_filters.ModelMultipleChoiceFilter(
        queryset=_vrf_qs,
        field_name='default_scope_vrf',
        label='Default Scope VRF',
    )

    class Meta:
        model = DHCPServer
        fields = ('name', 'hostname', 'port', 'use_https', 'health_status', 'access_level', 'maintenance_mode')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) |
            Q(hostname__icontains=value)
        )


@register_filterset
class DHCPFailoverFilterSet(NetBoxModelFilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    primary_server_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DHCPServer.objects.all(),
        field_name='primary_server',
        label='Primary Server',
    )
    secondary_server_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DHCPServer.objects.all(),
        field_name='secondary_server',
        label='Secondary Server',
    )
    mode = django_filters.MultipleChoiceFilter(
        choices=DHCPFailover.MODE_CHOICES,
    )
    default_scope_vrf_id = django_filters.ModelMultipleChoiceFilter(
        queryset=_vrf_qs,
        field_name='default_scope_vrf',
        label='Default Scope VRF',
    )

    class Meta:
        model = DHCPFailover
        fields = ('name', 'description', 'mode', 'enable_auth', 'sync_enabled', 'maintenance_mode')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) |
            Q(description__icontains=value) |
            Q(primary_server__name__icontains=value) |
            Q(secondary_server__name__icontains=value)
        )


@register_filterset
class DHCPOptionCodeDefinitionFilterSet(NetBoxModelFilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    description = django_filters.CharFilter(lookup_expr='icontains')
    data_type = django_filters.MultipleChoiceFilter(
        choices=DHCPOptionCodeDefinition.DATA_TYPE_CHOICES,
    )
    is_builtin = django_filters.BooleanFilter()

    class Meta:
        model = DHCPOptionCodeDefinition
        fields = ('code', 'name', 'data_type', 'is_builtin', 'vendor_class')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        q = Q(name__icontains=value) | Q(vendor_class__icontains=value)
        try:
            q |= Q(code=int(value))
        except ValueError:
            pass
        return queryset.filter(q)


@register_filterset
class DHCPOptionValueFilterSet(NetBoxModelFilterSet):
    option_definition_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DHCPOptionCodeDefinition.objects.all(),
        field_name='option_definition',
        label='Option Definition',
    )
    friendly_name = django_filters.CharFilter(lookup_expr='icontains')
    value = django_filters.CharFilter(lookup_expr='icontains')

    class Meta:
        model = DHCPOptionValue
        fields = ('option_definition', 'friendly_name', 'value')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        q = (
            Q(friendly_name__icontains=value) |
            Q(value__icontains=value) |
            Q(option_definition__name__icontains=value)
        )
        try:
            q |= Q(option_definition__code=int(value))
        except ValueError:
            pass
        return queryset.filter(q)


@register_filterset
class DHCPExclusionRangeFilterSet(NetBoxModelFilterSet):
    scope_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DHCPScope.objects.all(),
        field_name='scope',
        label='Scope',
    )

    class Meta:
        model = DHCPExclusionRange
        fields = ('scope', 'start_ip', 'end_ip', 'description')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(start_ip__icontains=value) |
            Q(end_ip__icontains=value) |
            Q(description__icontains=value) |
            Q(scope__name__icontains=value)
        )


@register_filterset
class DHCPScopeFilterSet(NetBoxModelFilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    prefix_id = django_filters.NumberFilter(field_name='prefix')
    # has_prefix=false lists the Unassigned Scopes (no prefix).
    has_prefix = django_filters.BooleanFilter(
        field_name='prefix', lookup_expr='isnull', exclude=True, label='Has Prefix',
    )
    network = django_filters.CharFilter()
    server_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DHCPServer.objects.all(),
        field_name='server',
        label='Server',
    )
    failover_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DHCPFailover.objects.all(),
        field_name='failover',
        label='Failover',
    )
    site = django_filters.ModelMultipleChoiceFilter(
        field_name='prefix___site',
        queryset=_site_qs,
        label='Site',
    )
    location = django_filters.ModelMultipleChoiceFilter(
        field_name='prefix___location',
        queryset=_location_qs,
        label='Location',
    )
    vrf = django_filters.ModelMultipleChoiceFilter(
        field_name='prefix__vrf',
        queryset=_vrf_qs,
        label='VRF',
    )
    tenant = django_filters.ModelMultipleChoiceFilter(
        field_name='prefix__tenant',
        queryset=_tenant_qs,
        label='Tenant',
    )
    within_prefix = django_filters.CharFilter(
        method='filter_within_prefix',
        label='Within Prefix',
    )

    def filter_within_prefix(self, queryset, name, value):
        value = value.strip()
        if not value:
            return queryset
        try:
            return queryset.filter(prefix__prefix__net_contained_or_equal=value)
        except Exception:
            return queryset.none()

    class Meta:
        model = DHCPScope
        fields = (
            'name', 'active', 'description', 'prefix', 'network', 'prefix_length', 'lease_lifetime', 'server',
            'failover', 'router', 'site', 'location', 'vrf', 'tenant', 'within_prefix', 'maintenance_mode',
        )

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) |
            Q(description__icontains=value) |
            Q(network__icontains=value) |
            Q(start_ip__icontains=value) |
            Q(end_ip__icontains=value) |
            Q(router__icontains=value)
        )


def _ip_address_qs(request=None):
    from ipam.models import IPAddress
    return IPAddress.objects.all()



def _ip_status_choices():
    from ipam.choices import IPAddressStatusChoices
    return [(value, label) for value, label, *_ in IPAddressStatusChoices.CHOICES]


class DHCPLeaseInfoFilterSet(django_filters.FilterSet):
    """Lease info isn't a NetBox model, so this is a plain FilterSet (API only)."""
    ip_address_id = django_filters.ModelMultipleChoiceFilter(
        queryset=_ip_address_qs,
        field_name='ip_address',
        label='IP Address (ID)',
    )
    address = django_filters.CharFilter(method='filter_address', label='IP Address')
    lease_hostname = django_filters.CharFilter(lookup_expr='icontains')
    state_changed_after = django_filters.IsoDateTimeFilter(
        field_name='state_changed', lookup_expr='gte', label='Active/Inactive Since (on or after)',
    )
    state_changed_before = django_filters.IsoDateTimeFilter(
        field_name='state_changed', lookup_expr='lte', label='Active/Inactive Since (on or before)',
    )

    class Meta:
        model = DHCPLeaseInfo
        fields = (
            'ip_address_id', 'address', 'active', 'lease_hostname',
            'state_changed_after', 'state_changed_before',
        )

    def filter_address(self, queryset, name, value):
        value = value.strip()
        if not value:
            return queryset
        try:
            return queryset.filter(ip_address__address__net_host=value.split('/')[0])
        except Exception:
            return queryset.none()


def _tag_qs(request=None):
    from extras.models import Tag
    return Tag.objects.all()


@register_filterset
class DHCPLeaseFilterSet(BaseFilterSet):
    """
    Filters for the Leases page. The queryset it filters is annotated with `scope_pk`
    (see utils.with_scope). Every column of the table can be filtered.
    """
    q = django_filters.CharFilter(method='search', label='Search')
    status = django_filters.MultipleChoiceFilter(
        field_name='ip_address__status', choices=_ip_status_choices, label='Status',
    )
    scope_id = django_filters.ModelMultipleChoiceFilter(
        method='filter_scope', queryset=DHCPScope.objects.all(), label='Scope (ID)',
    )
    parent = MultiValueCharFilter(method='filter_parent', label='Parent Prefix')
    vrf_id = django_filters.ModelMultipleChoiceFilter(
        field_name='ip_address__vrf', queryset=_vrf_qs, label='VRF',
    )
    tenant_id = django_filters.ModelMultipleChoiceFilter(
        field_name='ip_address__tenant', queryset=_tenant_qs, label='Tenant',
    )
    # The "is not" forms (`__n`) are what the filter form's is / is not modifier sends.
    server_id = django_filters.ModelMultipleChoiceFilter(
        method='filter_server', queryset=DHCPServer.objects.all(), label='Server (ID)',
    )
    server_id__n = django_filters.ModelMultipleChoiceFilter(
        method='filter_server_not', queryset=DHCPServer.objects.all(), label='Server (ID) is not',
    )
    failover_id = django_filters.ModelMultipleChoiceFilter(
        method='filter_failover', queryset=DHCPFailover.objects.all(), label='Failover (ID)',
    )
    failover_id__n = django_filters.ModelMultipleChoiceFilter(
        method='filter_failover_not', queryset=DHCPFailover.objects.all(), label='Failover (ID) is not',
    )
    description = django_filters.CharFilter(field_name='ip_address__description', lookup_expr='icontains')
    dns_name = MultiValueCharFilter(field_name='ip_address__dns_name', label='DNS Name')
    client_id = django_filters.CharFilter(method='filter_client_id', label='Client ID')
    tag = django_filters.ModelMultipleChoiceFilter(
        field_name='ip_address__tags__slug', queryset=_tag_qs, to_field_name='slug', label='Tag',
    )
    state_changed_after = django_filters.DateTimeFilter(
        field_name='state_changed', lookup_expr='gte', label='Active/Inactive Since (on or after)',
    )
    state_changed_before = django_filters.DateTimeFilter(
        field_name='state_changed', lookup_expr='lte', label='Active/Inactive Since (on or before)',
    )
    expiration_after = django_filters.DateTimeFilter(
        field_name='lease_expiration', lookup_expr='gte', label='Expiration (on or after)',
    )
    expiration_before = django_filters.DateTimeFilter(
        field_name='lease_expiration', lookup_expr='lte', label='Expiration (on or before)',
    )

    class Meta:
        model = DHCPLeaseInfo
        fields = ('active', 'lease_hostname', 'lease_expiration', 'state_changed')

    def search(self, queryset, name, value):
        value = value.strip()
        if not value:
            return queryset
        return queryset.filter(
            Q(lease_hostname__icontains=value) |
            Q(ip_address__address__istartswith=value) |
            Q(ip_address__dns_name__icontains=value) |
            Q(ip_address__description__icontains=value) |
            Q(scope_pk__in=DHCPScope.objects.filter(name__icontains=value).values('pk'))
        )

    def filter_client_id(self, queryset, name, value):
        value = value.strip()
        if not value:
            return queryset
        return queryset.filter(ip_address__custom_field_data__dhcp_client_id__icontains=value)

    def filter_scope(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(scope_pk__in=[scope.pk for scope in value])

    def filter_parent(self, queryset, name, value):
        """Like the IP Addresses list's Parent Prefix: IPs inside any of the given prefixes."""
        if not value:
            return queryset
        q = Q()
        for prefix in value:
            try:
                q |= Q(ip_address__address__net_host_contained=str(netaddr.IPNetwork(prefix.strip()).cidr))
            except (netaddr.AddrFormatError, ValueError):
                return queryset.none()
        return queryset.filter(q)

    def filter_server(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(scope_pk__in=DHCPScope.objects.filter(server__in=value).values('pk'))

    def filter_server_not(self, queryset, name, value):
        if not value:
            return queryset
        # An IP with no scope isn't on that server, so "is not" keeps it (a NULL doesn't pass NOT IN).
        owned = DHCPScope.objects.filter(server__in=value).values('pk')
        return queryset.filter(Q(scope_pk__isnull=True) | ~Q(scope_pk__in=owned))

    def filter_failover(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(scope_pk__in=DHCPScope.objects.filter(failover__in=value).values('pk'))

    def filter_failover_not(self, queryset, name, value):
        if not value:
            return queryset
        # An IP with no scope isn't on that failover, so "is not" keeps it (a NULL doesn't pass NOT IN).
        owned = DHCPScope.objects.filter(failover__in=value).values('pk')
        return queryset.filter(Q(scope_pk__isnull=True) | ~Q(scope_pk__in=owned))
