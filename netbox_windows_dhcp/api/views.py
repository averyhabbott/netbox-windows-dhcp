from netbox.api.viewsets import NetBoxModelViewSet

from ..filtersets import (
    DHCPFailoverFilterSet,
    DHCPOptionCodeDefinitionFilterSet,
    DHCPOptionValueFilterSet,
    DHCPScopeFilterSet,
    DHCPServerFilterSet,
)
from ..models import (
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)
from .serializers import (
    DHCPFailoverSerializer,
    DHCPOptionCodeDefinitionSerializer,
    DHCPOptionValueSerializer,
    DHCPScopeSerializer,
    DHCPServerSerializer,
)


class DHCPServerViewSet(NetBoxModelViewSet):
    queryset = DHCPServer.objects.all()
    serializer_class = DHCPServerSerializer
    filterset_class = DHCPServerFilterSet


class DHCPFailoverViewSet(NetBoxModelViewSet):
    queryset = DHCPFailover.objects.select_related('primary_server', 'secondary_server')
    serializer_class = DHCPFailoverSerializer
    filterset_class = DHCPFailoverFilterSet


class DHCPOptionCodeDefinitionViewSet(NetBoxModelViewSet):
    queryset = DHCPOptionCodeDefinition.objects.all()
    serializer_class = DHCPOptionCodeDefinitionSerializer
    filterset_class = DHCPOptionCodeDefinitionFilterSet


class DHCPOptionValueViewSet(NetBoxModelViewSet):
    queryset = DHCPOptionValue.objects.select_related('option_definition')
    serializer_class = DHCPOptionValueSerializer
    filterset_class = DHCPOptionValueFilterSet


class DHCPScopeViewSet(NetBoxModelViewSet):
    queryset = DHCPScope.objects.select_related('prefix', 'failover').prefetch_related(
        'option_values__option_definition'
    )
    serializer_class = DHCPScopeSerializer
    filterset_class = DHCPScopeFilterSet
