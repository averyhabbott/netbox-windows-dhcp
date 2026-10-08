from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from netbox.api.fields import ChoiceField
from netbox.api.serializers import NetBoxModelSerializer
from ipam.api.serializers import IPAddressSerializer, PrefixSerializer, VRFSerializer
from ipam.models import VRF, Prefix
from users.api.serializers import UserSerializer

from ..choices import DHCPServerAccessChoices, DHCPServerHealthChoices
from ..locks import EXCLUSION_EDITABLE_WHEN_PUSH_OFF, SCOPE_EDITABLE_WHEN_PUSH_OFF, check_option_codes
from ..models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPLeaseInfo,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)

#: Maintenance fields every serializer shows. Who and when are filled in automatically.
MAINTENANCE_API_FIELDS = (
    'maintenance_mode', 'maintenance_notes', 'maintenance_enabled_at', 'maintenance_enabled_by',
)


def _refuse_server_owned_changes(serializer, data, editable):
    """
    While push_scope_info is off, refuse an edit that changes a field outside `editable`
    (the DHCP server owns those). A value sent unchanged is fine. Call it before
    NetBox's validate(), which copies `data` onto the instance.
    """
    from ..locks import server_owns_scope_info

    instance = serializer.instance
    if instance is None or not server_owns_scope_info():
        return

    def changed(field, value):
        if field == 'option_values':
            return {ov.pk for ov in value} != set(instance.option_values.values_list('pk', flat=True))
        return getattr(instance, field, value) != value

    locked = sorted(field for field, value in data.items() if field not in editable and changed(field, value))
    if locked:
        allowed = ', '.join(editable)
        raise serializers.ValidationError({
            field: f'Set on the DHCP server — read-only while "Push Scope Info" is disabled. '
                   f'Only {allowed} can be changed.'
            for field in locked
        })


def _fill_maintenance(serializer, data):
    """
    Fill in who and when for a maintenance change, the same way as the UI: turning
    maintenance on records the request's user and the time, turning it off clears the
    notes, user and time. With maintenance staying on, only the notes change; staying
    off, notes are cleared. Call it after any field-lock check (who/when aren't sent).
    """
    from ..utils import maintenance_fields

    if 'maintenance_mode' not in data and 'maintenance_notes' not in data:
        return
    was_on = bool(getattr(serializer.instance, 'maintenance_mode', False))
    on = data.get('maintenance_mode', was_on)
    if on and was_on:
        return
    if not on and not was_on:
        data['maintenance_notes'] = ''
        return
    request = serializer.context.get('request')
    user = getattr(request, 'user', None)
    if not getattr(user, 'is_authenticated', False):
        user = None
    data.update(maintenance_fields(on, data.get('maintenance_notes', ''), user))


class DHCPServerSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpserver-detail'
    )
    # Status fields below are maintained by the health check / sync jobs — read-only via the API.
    health_status = ChoiceField(choices=DHCPServerHealthChoices, read_only=True)
    access_level = ChoiceField(choices=DHCPServerAccessChoices, read_only=True)
    default_scope_vrf = VRFSerializer(nested=True, read_only=True, allow_null=True)
    default_scope_vrf_id = serializers.PrimaryKeyRelatedField(
        queryset=VRF.objects.all(),
        source='default_scope_vrf',
        write_only=True,
        required=False,
        allow_null=True,
    )
    # Whether an HTTPS certificate is stored ("Import HTTPS Certificate"); the certificate
    # itself isn't shown, and it's imported or removed only from the UI.
    has_ca_cert = serializers.SerializerMethodField()
    maintenance_enabled_by = UserSerializer(nested=True, read_only=True)

    class Meta:
        model = DHCPServer
        fields = (
            'id', 'url', 'display', 'name', 'hostname', 'port', 'use_https',
            'api_key', 'verify_ssl', 'has_ca_cert', 'ca_cert_expiry', 'sync_standalone_scopes',
            'default_scope_vrf', 'default_scope_vrf_id',
            'health_status', 'access_level', 'last_health_check', 'health_error',
            'last_sync_at', 'last_sync_error', 'psu_script_version',
            *MAINTENANCE_API_FIELDS,
            'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'hostname')
        read_only_fields = (
            'ca_cert_expiry', 'last_health_check', 'health_error', 'last_sync_at', 'last_sync_error',
            'psu_script_version', 'maintenance_enabled_at',
        )
        extra_kwargs = {
            'api_key': {'write_only': True},
        }

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_has_ca_cert(self, obj):
        return bool(obj.ca_cert)

    def validate(self, data):
        _fill_maintenance(self, data)
        return super().validate(data)


class DHCPFailoverSerializer(NetBoxModelSerializer):
    """
    Failovers are managed on the DHCP server and imported, so the API can't create them
    (see DHCPFailoverViewSet), and an edit may only change the NetBox-side fields:
    Default Scope VRF, description, sync enabled, maintenance mode and notes, tags and
    custom fields. Changing anything else is refused. The shared secret is never exposed.
    """
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpfailover-detail'
    )
    primary_server = DHCPServerSerializer(nested=True, read_only=True)
    primary_server_id = serializers.PrimaryKeyRelatedField(
        queryset=DHCPServer.objects.all(),
        source='primary_server',
        write_only=True,
        required=False,
    )
    secondary_server = DHCPServerSerializer(nested=True, read_only=True)
    secondary_server_id = serializers.PrimaryKeyRelatedField(
        queryset=DHCPServer.objects.all(),
        source='secondary_server',
        write_only=True,
        required=False,
    )
    default_scope_vrf = VRFSerializer(nested=True, read_only=True, allow_null=True)
    default_scope_vrf_id = serializers.PrimaryKeyRelatedField(
        queryset=VRF.objects.all(),
        source='default_scope_vrf',
        write_only=True,
        required=False,
        allow_null=True,
    )
    maintenance_enabled_by = UserSerializer(nested=True, read_only=True)

    #: The only fields an edit may change.
    EDITABLE_FIELDS = (
        'default_scope_vrf', 'description', 'sync_enabled', 'maintenance_mode', 'maintenance_notes',
        'tags', 'custom_fields',
    )

    class Meta:
        model = DHCPFailover
        fields = (
            'id', 'url', 'display', 'name', 'description',
            'primary_server', 'primary_server_id',
            'secondary_server', 'secondary_server_id',
            'mode', 'max_client_lead_time', 'max_response_delay',
            'state_switchover_interval', 'sync_enabled', 'enable_auth',
            'default_scope_vrf', 'default_scope_vrf_id',
            *MAINTENANCE_API_FIELDS,
            'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'mode')
        read_only_fields = ('maintenance_enabled_at',)

    def validate(self, data):
        # Before super(): NetBox's validate() copies `data` onto the instance.
        if self.instance is not None:
            locked = sorted(
                field for field, value in data.items()
                if field not in self.EDITABLE_FIELDS and getattr(self.instance, field, value) != value
            )
            if locked:
                allowed = ', '.join(self.EDITABLE_FIELDS)
                raise serializers.ValidationError({
                    field: f'Set on the DHCP server — read-only in NetBox. Only {allowed} can be changed.'
                    for field in locked
                })
        _fill_maintenance(self, data)
        return super().validate(data)


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
        # Built-in codes ship with the plugin; the API can't make or unmake one.
        read_only_fields = ('is_builtin',)


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
            'start_ip', 'end_ip', 'description',
            'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'start_ip', 'end_ip')

    def validate(self, data):
        _refuse_server_owned_changes(self, data, EXCLUSION_EDITABLE_WHEN_PUSH_OFF)
        return super().validate(data)


class DHCPScopeSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpscope-detail'
    )
    prefix = PrefixSerializer(nested=True, read_only=True, allow_null=True)
    # Optional: a scope with no prefix needs network and prefix_length instead.
    prefix_id = serializers.PrimaryKeyRelatedField(
        queryset=Prefix.objects.all(),
        source='prefix',
        write_only=True,
        required=False,
        allow_null=True,
    )
    network = serializers.IPAddressField(protocol='IPv4', required=False)
    prefix_length = serializers.IntegerField(min_value=1, max_value=32, required=False)
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
    maintenance_enabled_by = UserSerializer(nested=True, read_only=True)

    class Meta:
        model = DHCPScope
        fields = (
            'id', 'url', 'display', 'name', 'active', 'description',
            'prefix', 'prefix_id', 'network', 'prefix_length',
            'start_ip', 'end_ip', 'router', 'lease_lifetime',
            'server', 'server_id',
            'failover', 'failover_id',
            'option_values', 'option_value_ids',
            'exclusion_ranges',
            *MAINTENANCE_API_FIELDS,
            'tags', 'custom_fields', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'start_ip', 'end_ip')
        read_only_fields = ('maintenance_enabled_at',)

    def validate(self, data):
        _refuse_server_owned_changes(self, data, SCOPE_EDITABLE_WHEN_PUSH_OFF)
        if 'option_values' in data:
            check_option_codes(data['option_values'])
        _fill_maintenance(self, data)
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


class DHCPLeaseInfoSerializer(serializers.ModelSerializer):
    """Read-only DHCP lease details the sync records for an IP Address."""
    url = serializers.HyperlinkedIdentityField(
        view_name='plugins-api:netbox_windows_dhcp-api:dhcpleaseinfo-detail'
    )
    ip_address = IPAddressSerializer(nested=True, read_only=True)

    class Meta:
        model = DHCPLeaseInfo
        fields = (
            'id', 'url', 'ip_address', 'lease_hostname', 'active', 'lease_expiration',
            'state_changed',
        )
        read_only_fields = fields
