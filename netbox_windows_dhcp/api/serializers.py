from rest_framework import serializers

from netbox.api.serializers import NetBoxModelSerializer
from ipam.api.serializers import PrefixSerializer
from ipam.models import Prefix

from ..models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)


class DHCPServerSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpserver-detail'
    )

    class Meta:
        model = DHCPServer
        fields = (
            'id', 'url', 'display', 'name', 'hostname', 'port', 'use_https',
            'api_key', 'verify_ssl', 'sync_standalone_scopes',
            'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'hostname')
        extra_kwargs = {
            'api_key': {'write_only': True},
        }


class DHCPFailoverSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpfailover-detail'
    )
    primary_server = DHCPServerSerializer(nested=True, read_only=True)
    primary_server_id = serializers.PrimaryKeyRelatedField(
        queryset=DHCPServer.objects.all(),
        source='primary_server',
        write_only=True,
    )
    secondary_server = DHCPServerSerializer(nested=True, read_only=True)
    secondary_server_id = serializers.PrimaryKeyRelatedField(
        queryset=DHCPServer.objects.all(),
        source='secondary_server',
        write_only=True,
    )

    class Meta:
        model = DHCPFailover
        fields = (
            'id', 'url', 'display', 'name',
            'primary_server', 'primary_server_id',
            'secondary_server', 'secondary_server_id',
            'mode', 'max_client_lead_time', 'max_response_delay',
            'state_switchover_interval', 'sync_enabled', 'enable_auth', 'shared_secret',
            'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'mode')
        extra_kwargs = {
            'shared_secret': {'write_only': True},
        }


class DHCPOptionCodeDefinitionSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpoptioncodedefinition-detail'
    )

    class Meta:
        model = DHCPOptionCodeDefinition
        fields = (
            'id', 'url', 'display', 'code', 'name', 'data_type',
            'description', 'is_builtin', 'vendor_class',
            'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'code', 'name', 'data_type')


class DHCPOptionValueSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpoptionvalue-detail'
    )
    option_definition = DHCPOptionCodeDefinitionSerializer(nested=True, read_only=True)
    option_definition_id = serializers.PrimaryKeyRelatedField(
        queryset=DHCPOptionCodeDefinition.objects.all(),
        source='option_definition',
        write_only=True,
    )

    class Meta:
        model = DHCPOptionValue
        fields = (
            'id', 'url', 'display',
            'option_definition', 'option_definition_id',
            'value', 'friendly_name',
            'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'friendly_name', 'value')


class _ScopeBriefSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpscope-detail'
    )

    class Meta:
        model = DHCPScope
        fields = ('id', 'url', 'display', 'name')
        brief_fields = ('id', 'url', 'display', 'name')


class DHCPExclusionRangeSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpexclusionrange-detail'
    )
    scope = _ScopeBriefSerializer(nested=True, read_only=True)
    scope_id = serializers.PrimaryKeyRelatedField(
        queryset=DHCPScope.objects.all(),
        source='scope',
        write_only=True,
    )

    class Meta:
        model = DHCPExclusionRange
        fields = (
            'id', 'url', 'display',
            'scope', 'scope_id',
            'start_ip', 'end_ip',
            'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'start_ip', 'end_ip')


class DHCPScopeSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpscope-detail'
    )
    prefix = PrefixSerializer(nested=True, read_only=True)
    prefix_id = serializers.PrimaryKeyRelatedField(
        queryset=Prefix.objects.all(),
        source='prefix',
        write_only=True,
    )
    server = DHCPServerSerializer(nested=True, read_only=True)
    server_id = serializers.PrimaryKeyRelatedField(
        queryset=DHCPServer.objects.all(),
        source='server',
        write_only=True,
        required=False,
        allow_null=True,
    )
    failover = DHCPFailoverSerializer(nested=True, read_only=True)
    failover_id = serializers.PrimaryKeyRelatedField(
        queryset=DHCPFailover.objects.all(),
        source='failover',
        write_only=True,
        required=False,
        allow_null=True,
    )
    option_values = DHCPOptionValueSerializer(nested=True, many=True, read_only=True)
    option_value_ids = serializers.PrimaryKeyRelatedField(
        queryset=DHCPOptionValue.objects.all(),
        source='option_values',
        many=True,
        write_only=True,
        required=False,
    )
    exclusion_ranges = DHCPExclusionRangeSerializer(many=True, read_only=True)

    class Meta:
        model = DHCPScope
        fields = (
            'id', 'url', 'display', 'name',
            'prefix', 'prefix_id',
            'start_ip', 'end_ip', 'router', 'lease_lifetime',
            'server', 'server_id',
            'failover', 'failover_id',
            'option_values', 'option_value_ids',
            'exclusion_ranges',
            'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'start_ip', 'end_ip')

    def validate(self, data):
        data = super().validate(data)
        # Fall back to the existing instance for fields absent from the payload so
        # partial updates (PATCH) that don't touch server/failover still validate.
        server = data.get('server', getattr(self.instance, 'server', None))
        failover = data.get('failover', getattr(self.instance, 'failover', None))
        if server and failover:
            raise serializers.ValidationError(
                'Set either server_id or failover_id, not both.'
            )
        if not server and not failover:
            raise serializers.ValidationError(
                'Either server_id or failover_id must be set.'
            )
        return data
