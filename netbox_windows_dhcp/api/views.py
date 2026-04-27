from netbox.api.viewsets import NetBoxModelViewSet

from ..filtersets import (
    DHCPExclusionRangeFilterSet,
    DHCPFailoverFilterSet,
    DHCPOptionCodeDefinitionFilterSet,
    DHCPOptionValueFilterSet,
    DHCPScopeFilterSet,
    DHCPServerFilterSet,
)
from ..models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)
from .permissions import DHCPAPIEnabled
from .serializers import (
    DHCPExclusionRangeSerializer,
    DHCPFailoverSerializer,
    DHCPOptionCodeDefinitionSerializer,
    DHCPOptionValueSerializer,
    DHCPScopeSerializer,
    DHCPServerSerializer,
)


class _DHCPBaseViewSet(NetBoxModelViewSet):
    """All plugin ViewSets inherit this to pick up the api_enabled gate."""

    def get_permissions(self):
        return [DHCPAPIEnabled()] + super().get_permissions()


class DHCPServerViewSet(_DHCPBaseViewSet):
    queryset = DHCPServer.objects.all()
    serializer_class = DHCPServerSerializer
    filterset_class = DHCPServerFilterSet


class DHCPFailoverViewSet(_DHCPBaseViewSet):
    queryset = DHCPFailover.objects.select_related('primary_server', 'secondary_server')
    serializer_class = DHCPFailoverSerializer
    filterset_class = DHCPFailoverFilterSet


class DHCPOptionCodeDefinitionViewSet(_DHCPBaseViewSet):
    queryset = DHCPOptionCodeDefinition.objects.all()
    serializer_class = DHCPOptionCodeDefinitionSerializer
    filterset_class = DHCPOptionCodeDefinitionFilterSet


class DHCPOptionValueViewSet(_DHCPBaseViewSet):
    queryset = DHCPOptionValue.objects.select_related('option_definition')
    serializer_class = DHCPOptionValueSerializer
    filterset_class = DHCPOptionValueFilterSet


class DHCPScopeViewSet(_DHCPBaseViewSet):
    queryset = DHCPScope.objects.select_related('prefix', 'failover').prefetch_related(
        'option_values__option_definition',
        'exclusion_ranges',
    )
    serializer_class = DHCPScopeSerializer
    filterset_class = DHCPScopeFilterSet


class DHCPExclusionRangeViewSet(_DHCPBaseViewSet):
    queryset = DHCPExclusionRange.objects.select_related('scope__prefix')
    serializer_class = DHCPExclusionRangeSerializer
    filterset_class = DHCPExclusionRangeFilterSet
