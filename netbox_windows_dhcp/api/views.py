from rest_framework import mixins, viewsets

from netbox.api.viewsets import NetBoxModelViewSet

from ..filtersets import (
    DHCPExclusionRangeFilterSet,
    DHCPFailoverFilterSet,
    DHCPLeaseInfoFilterSet,
    DHCPOptionCodeDefinitionFilterSet,
    DHCPOptionValueFilterSet,
    DHCPScopeFilterSet,
    DHCPServerFilterSet,
)
from ..models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPLeaseInfo,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)
from .permissions import DHCPAPIEnabled, LeaseInfoReadable, ScopeInfoWritable
from .serializers import (
    DHCPExclusionRangeSerializer,
    DHCPFailoverSerializer,
    DHCPLeaseInfoSerializer,
    DHCPOptionCodeDefinitionSerializer,
    DHCPOptionValueSerializer,
    DHCPScopeSerializer,
    DHCPServerSerializer,
)


class _DHCPBaseViewSet(NetBoxModelViewSet):
    """All plugin ViewSets inherit this to pick up the api_enabled gate."""

    def get_permissions(self):
        return [DHCPAPIEnabled()] + super().get_permissions()


class _ScopeInfoViewSet(_DHCPBaseViewSet):
    """Scope-info models: most writes are refused while push_scope_info is off (see ScopeInfoWritable)."""

    def get_permissions(self):
        return super().get_permissions() + [ScopeInfoWritable()]


class DHCPServerViewSet(_DHCPBaseViewSet):
    queryset = DHCPServer.objects.all()
    serializer_class = DHCPServerSerializer
    filterset_class = DHCPServerFilterSet


class DHCPFailoverViewSet(_DHCPBaseViewSet):
    queryset = DHCPFailover.objects.select_related('primary_server', 'secondary_server', 'default_scope_vrf')
    serializer_class = DHCPFailoverSerializer
    filterset_class = DHCPFailoverFilterSet

    def create(self, request, *args, **kwargs):
        # The same rule as the UI: failovers only come from "Import from Server".
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied(
            detail='Failover relationships are read-only. Import them from a DHCP server to create them.'
        )


class DHCPOptionCodeDefinitionViewSet(_DHCPBaseViewSet):
    queryset = DHCPOptionCodeDefinition.objects.all()
    serializer_class = DHCPOptionCodeDefinitionSerializer
    filterset_class = DHCPOptionCodeDefinitionFilterSet


class DHCPOptionValueViewSet(_ScopeInfoViewSet):
    queryset = DHCPOptionValue.objects.select_related('option_definition')
    serializer_class = DHCPOptionValueSerializer
    filterset_class = DHCPOptionValueFilterSet


class DHCPScopeViewSet(_ScopeInfoViewSet):
    queryset = DHCPScope.objects.select_related('prefix', 'failover').prefetch_related(
        'option_values__option_definition',
        'exclusion_ranges',
    )
    serializer_class = DHCPScopeSerializer
    filterset_class = DHCPScopeFilterSet
    # With push_scope_info off, edits may change the NetBox-only fields (serializer).
    push_off_editable = True


class DHCPExclusionRangeViewSet(_ScopeInfoViewSet):
    queryset = DHCPExclusionRange.objects.select_related('scope__prefix')
    serializer_class = DHCPExclusionRangeSerializer
    filterset_class = DHCPExclusionRangeFilterSet
    # With push_scope_info off, edits may change the NetBox-only fields (serializer).
    push_off_editable = True


class DHCPLeaseInfoViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    Read-only: the DHCP lease details the sync records. They have no permissions of their
    own; you see an entry only if you may view its IP Address, the same as the panel on
    the IP Address page.
    """
    queryset = DHCPLeaseInfo.objects.select_related('ip_address')
    serializer_class = DHCPLeaseInfoSerializer
    filterset_class = DHCPLeaseInfoFilterSet
    permission_classes = (DHCPAPIEnabled, LeaseInfoReadable)

    def get_queryset(self):
        from ipam.models import IPAddress
        viewable = IPAddress.objects.restrict(self.request.user, 'view')
        return super().get_queryset().filter(ip_address__in=viewable).order_by('ip_address__address', 'pk')
