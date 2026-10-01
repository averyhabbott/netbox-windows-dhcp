import django_tables2 as tables
from django.utils.html import format_html
from netbox.tables import NetBoxTable, BooleanColumn, ActionsColumn, TagColumn

from .models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)

_MAINTENANCE_COLUMN = tables.TemplateColumn(
    template_code=(
        '{% if record.maintenance_mode %}'
        '<span class="text-azure" title="In Maintenance">'
        '<i class="mdi mdi-pause-circle"></i>'
        '</span>'
        '{% endif %}'
    ),
    verbose_name='Maint.',
    orderable=True,
    attrs={'td': {'style': 'width:30px; text-align:center'}},
)


class DHCPServerTable(NetBoxTable):
    name = tables.Column(linkify=True)
    hostname = tables.Column()
    port = tables.Column()
    use_https = BooleanColumn(verbose_name='HTTPS')
    verify_ssl = BooleanColumn(verbose_name='SSL Verify')
    access_level = tables.TemplateColumn(
        template_code=(
            '{% if record.access_level == "rw" %}'
            '<span class="badge text-bg-success">Read-Write</span>'
            '{% elif record.access_level == "ro" %}'
            '<span class="badge text-bg-info">Read-Only</span>'
            '{% else %}'
            '<span class="badge text-bg-secondary">Unknown</span>'
            '{% endif %}'
        ),
        verbose_name='Writable',
        orderable=True,
    )
    sync_standalone_scopes = BooleanColumn(verbose_name='Sync Standalone')
    default_scope_vrf = tables.Column(linkify=True, verbose_name='Default Scope VRF')
    has_api_key = tables.Column(
        accessor='api_key',
        verbose_name='API Key',
        orderable=False,
    )
    health_status = tables.TemplateColumn(
        template_code=(
            '{% if record.health_status == "healthy" %}'
            '<span class="badge text-bg-success">Healthy</span>'
            '{% elif record.health_status == "unreachable" %}'
            '<span class="badge text-bg-danger">Unreachable</span>'
            '{% else %}'
            '<span class="badge text-bg-secondary">Unknown</span>'
            '{% endif %}'
        ),
        verbose_name='Health',
        orderable=True,
    )
    psu_script_version = tables.Column(verbose_name='Remote Scripts Version', orderable=False)
    maintenance_mode = _MAINTENANCE_COLUMN
    actions = ActionsColumn(
        extra_buttons=(
            '<a href="{% url \'plugins:netbox_windows_dhcp:dhcpserver_maintenance\' record.pk %}"'
            '   class="btn btn-sm btn-azure" title="Maintenance Mode">'
            '<i class="mdi mdi-pause-circle-outline"></i>'
            '</a>'
            # A submit button, not a link: the list table sits inside NetBox's POST form (with its CSRF token)
            '<button type="submit" formaction="{% url \'plugins:netbox_windows_dhcp:dhcpserver_sync\' record.pk %}"'
            '   class="btn btn-sm btn-primary" title="Sync Now">'
            '<i class="mdi mdi-sync"></i>'
            '</button>'
        ),
    )

    class Meta(NetBoxTable.Meta):
        model = DHCPServer
        fields = (
            'pk', 'name', 'hostname', 'port', 'use_https', 'verify_ssl', 'access_level',
            'sync_standalone_scopes', 'default_scope_vrf', 'has_api_key',
            'health_status', 'psu_script_version', 'maintenance_mode', 'actions',
        )
        default_columns = (
            'name', 'hostname', 'use_https', 'verify_ssl', 'access_level', 'sync_standalone_scopes',
            'health_status', 'maintenance_mode', 'actions',
        )

    def render_has_api_key(self, value):
        return 'Yes' if value else 'No'

    def render_psu_script_version(self, value):
        from .constants import PSU_SCRIPT_VERSION
        if not value:
            return format_html(
                '<span class="text-secondary" title="No version recorded">'
                '<i class="mdi mdi-help-circle-outline"></i></span>'
            )
        if value == PSU_SCRIPT_VERSION:
            return format_html(
                '<span class="text-success"><i class="mdi mdi-check-circle-outline"></i> {}</span>',
                value,
            )
        return format_html(
            '<span class="text-warning" title="Expected {}">'
            '<i class="mdi mdi-alert-outline"></i> {}</span>',
            PSU_SCRIPT_VERSION,
            value,
        )


class DHCPFailoverTable(NetBoxTable):
    name = tables.Column(linkify=True)
    primary_server = tables.Column(linkify=True)
    secondary_server = tables.Column(linkify=True)
    mode = tables.Column()
    enable_auth = BooleanColumn(verbose_name='Auth')
    sync_enabled = BooleanColumn(verbose_name='Sync')
    default_scope_vrf = tables.Column(linkify=True, verbose_name='Default Scope VRF')
    maintenance_mode = _MAINTENANCE_COLUMN
    actions = ActionsColumn(
        actions=('edit', 'delete', 'changelog'),
        extra_buttons=(
            '<a href="{% url \'plugins:netbox_windows_dhcp:dhcpfailover_maintenance\' record.pk %}"'
            '   class="btn btn-sm btn-azure" title="Maintenance Mode">'
            '<i class="mdi mdi-pause-circle-outline"></i>'
            '</a>'
            '<button type="submit"'
            '        formaction="{% url \'plugins:netbox_windows_dhcp:dhcpfailover_toggle_sync\' record.pk %}"'
            '        class="btn btn-sm {% if record.sync_enabled %}btn-success{% else %}btn-secondary{% endif %}"'
            '        title="Toggle Sync">'
            '  <i class="mdi mdi-sync{% if not record.sync_enabled %}-off{% endif %}"></i>'
            '</button>'
        ),
    )

    class Meta(NetBoxTable.Meta):
        model = DHCPFailover
        fields = (
            'pk', 'name', 'description', 'primary_server', 'secondary_server',
            'mode', 'max_client_lead_time', 'max_response_delay',
            'enable_auth', 'sync_enabled', 'default_scope_vrf', 'maintenance_mode', 'actions',
        )
        default_columns = (
            'name', 'primary_server', 'secondary_server', 'mode',
            'sync_enabled', 'maintenance_mode', 'actions',
        )


class DHCPOptionCodeDefinitionTable(NetBoxTable):
    code = tables.Column(linkify=True)
    name = tables.Column(linkify=True)
    data_type = tables.Column()
    is_builtin = BooleanColumn(verbose_name='Built-in')

    class Meta(NetBoxTable.Meta):
        model = DHCPOptionCodeDefinition
        fields = ('pk', 'code', 'name', 'data_type', 'is_builtin', 'vendor_class', 'actions')
        default_columns = ('code', 'name', 'data_type', 'is_builtin', 'actions')


class DHCPOptionValueTable(NetBoxTable):
    friendly_name = tables.Column(
        linkify=lambda record: record.get_absolute_url(),
        verbose_name='Friendly Name',
        empty_values=(),
    )
    option_definition = tables.Column(
        linkify=True,
        verbose_name='Option Code',
    )
    value = tables.Column()

    class Meta(NetBoxTable.Meta):
        model = DHCPOptionValue
        fields = ('pk', 'friendly_name', 'option_definition', 'value', 'actions')
        default_columns = ('friendly_name', 'option_definition', 'value', 'actions')

    def render_friendly_name(self, value, record):
        return value or str(record)


class DHCPExclusionRangeTable(NetBoxTable):
    start_ip = tables.Column(verbose_name='Start IP')
    end_ip = tables.Column(verbose_name='End IP')
    scope = tables.Column(linkify=True, verbose_name='Scope')

    class Meta(NetBoxTable.Meta):
        model = DHCPExclusionRange
        fields = ('pk', 'start_ip', 'end_ip', 'scope', 'description', 'actions')
        default_columns = ('start_ip', 'end_ip', 'description', 'actions')


class DHCPScopeTable(NetBoxTable):
    name = tables.Column(linkify=True)
    active = BooleanColumn(verbose_name='Active')
    network = tables.TemplateColumn(
        template_code='{{ record.network }}/{{ record.prefix_length }}',
        verbose_name='Network',
        order_by=('network', 'prefix_length'),
    )
    prefix = tables.Column(linkify=True)
    start_ip = tables.Column(verbose_name='Start IP')
    end_ip = tables.Column(verbose_name='End IP')
    router = tables.Column(verbose_name='Router')
    source = tables.Column(
        verbose_name='Source',
        accessor='pk',
        orderable=False,
    )
    lease_lifetime = tables.Column(verbose_name='Lease Life')
    maintenance_mode = _MAINTENANCE_COLUMN
    actions = ActionsColumn(
        extra_buttons=(
            '<a href="{% url \'plugins:netbox_windows_dhcp:dhcpscope_maintenance\' record.pk %}"'
            '   class="btn btn-sm btn-azure" title="Maintenance Mode">'
            '<i class="mdi mdi-pause-circle-outline"></i>'
            '</a>'
        ),
    )

    def render_lease_lifetime(self, value):
        from .utils import lease_lifetime_display
        return lease_lifetime_display(value)

    def render_source(self, record):
        if record.failover_id:
            return format_html(
                '<a href="{}">{}</a>', record.failover.get_absolute_url(), record.failover
            )
        if record.server_id:
            return format_html(
                '<a href="{}">{}</a>', record.server.get_absolute_url(), record.server
            )
        return '—'

    tags = TagColumn(url_name='plugins:netbox_windows_dhcp:dhcpscope_list')

    class Meta(NetBoxTable.Meta):
        model = DHCPScope
        fields = (
            'pk', 'name', 'active', 'description', 'network', 'prefix', 'start_ip', 'end_ip',
            'router', 'source', 'lease_lifetime', 'tags', 'maintenance_mode', 'actions',
        )
        default_columns = (
            'name', 'active', 'network', 'prefix', 'start_ip', 'end_ip', 'source', 'tags', 'maintenance_mode', 'actions',
        )


# ---------------------------------------------------------------------------
# Extra column on NetBox's core IP Addresses table (opt-in via "Configure Table")
# ---------------------------------------------------------------------------

def register_core_table_columns():
    """Called once from PluginConfig.ready()."""
    from ipam.tables import IPAddressTable
    from utilities.tables import register_table_column

    register_table_column(
        tables.Column(
            verbose_name='Lease Hostname',
            accessor=tables.A('dhcp_lease_info__lease_hostname'),
            order_by=('dhcp_lease_info__lease_hostname',),
        ),
        'dhcp_lease_hostname',
        IPAddressTable,
    )
