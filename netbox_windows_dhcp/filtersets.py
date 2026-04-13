import django_filters
from django.db.models import Q
from netbox.filtersets import NetBoxModelFilterSet
from utilities.filtersets import register_filterset

from .models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)


@register_filterset
class DHCPServerFilterSet(NetBoxModelFilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    hostname = django_filters.CharFilter(lookup_expr='icontains')

    class Meta:
        model = DHCPServer
        fields = ('name', 'hostname', 'port', 'use_https')

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

    class Meta:
        model = DHCPFailover
        fields = ('name', 'mode', 'enable_auth')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) |
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
        return queryset.filter(
            Q(friendly_name__icontains=value) |
            Q(value__icontains=value) |
            Q(option_definition__name__icontains=value)
        )


@register_filterset
class DHCPExclusionRangeFilterSet(NetBoxModelFilterSet):
    scope_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DHCPScope.objects.all(),
        field_name='scope',
        label='Scope',
    )

    class Meta:
        model = DHCPExclusionRange
        fields = ('scope', 'start_ip', 'end_ip')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(start_ip__icontains=value) |
            Q(end_ip__icontains=value) |
            Q(scope__name__icontains=value)
        )


@register_filterset
class DHCPScopeFilterSet(NetBoxModelFilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    prefix_id = django_filters.NumberFilter(field_name='prefix')
    failover_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DHCPFailover.objects.all(),
        field_name='failover',
        label='Failover',
    )

    class Meta:
        model = DHCPScope
        fields = ('name', 'prefix', 'failover', 'router')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value) |
            Q(start_ip__icontains=value) |
            Q(end_ip__icontains=value) |
            Q(router__icontains=value)
        )
