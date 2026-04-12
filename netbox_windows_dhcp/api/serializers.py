from rest_framework import serializers

from netbox.api.serializers import NetBoxModelSerializer
from ipam.models import Prefix

from ..models import (
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)


class BriefPrefixSerializer(serializers.ModelSerializer):
    """Minimal read-only Prefix representation for nested use in DHCPScopeSerializer."""
    url = serializers.HyperlinkedIdentityField(view_name='ipam-api:prefix-detail')

    class Meta:
        model = Prefix
        fields = ('id', 'url', 'display', 'prefix')


class DHCPServerSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpserver-detail'
    )

    class Meta:
        model = DHCPServer
        fields = (
            'id', 'url', 'display', 'name', 'hostname', 'port', 'use_https',
            'api_key', 'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'hostname')
        extra_kwargs = {
            'api_key': {'write_only': True},
        }


class DHCPFailoverSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpfailover-detail'
    )
    primary_server = DHCPServerSerializer(nested=True)
    primary_server_id = serializers.PrimaryKeyRelatedField(
        queryset=DHCPServer.objects.all(),
        source='primary_server',
        write_only=True,
    )
    secondary_server = DHCPServerSerializer(nested=True)
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
            'state_switchover_interval', 'enable_auth', 'shared_secret',
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
    option_definition = DHCPOptionCodeDefinitionSerializer(nested=True)
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


class DHCPScopeSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpscope-detail'
    )
    prefix = BriefPrefixSerializer(read_only=True)
    prefix_id = serializers.PrimaryKeyRelatedField(
        queryset=Prefix.objects.all(),
        source='prefix',
        write_only=True,
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

    class Meta:
        model = DHCPScope
        fields = (
            'id', 'url', 'display', 'name',
            'prefix', 'prefix_id',
            'start_ip', 'end_ip', 'router', 'lease_lifetime',
            'failover', 'failover_id',
            'option_values', 'option_value_ids',
            'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'start_ip', 'end_ip')
