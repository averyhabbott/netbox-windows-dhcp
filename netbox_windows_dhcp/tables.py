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


class DHCPServerTable(NetBoxTable):
    name = tables.Column(linkify=True)
    hostname = tables.Column()
    port = tables.Column()
    use_https = BooleanColumn(verbose_name='HTTPS')
    verify_ssl = BooleanColumn(verbose_name='SSL Verify')
    sync_standalone_scopes = BooleanColumn(verbose_name='Sync Standalone')
    has_api_key = tables.Column(
        accessor='api_key',
        verbose_name='API Key',
        orderable=False,
    )
    actions = ActionsColumn(
        extra_buttons="""
            <a href="{% url 'plugins:netbox_windows_dhcp:dhcpserver_sync' record.pk %}"
               class="btn btn-sm btn-primary"
               title="Sync Now">
                <i class="mdi mdi-sync"></i>
            </a>
        """,
    )

    class Meta(NetBoxTable.Meta):
        model = DHCPServer
        fields = ('pk', 'name', 'hostname', 'port', 'use_https', 'verify_ssl', 'sync_standalone_scopes', 'has_api_key', 'actions')
        default_columns = ('name', 'hostname', 'use_https', 'verify_ssl', 'sync_standalone_scopes', 'actions')

    def render_has_api_key(self, value):
        return 'Yes' if value else 'No'


class DHCPFailoverTable(NetBoxTable):
    name = tables.Column(linkify=True)
    primary_server = tables.Column(linkify=True)
    secondary_server = tables.Column(linkify=True)
    mode = tables.Column()
    enable_auth = BooleanColumn(verbose_name='Auth')
    sync_enabled = BooleanColumn(verbose_name='Sync')
    actions = ActionsColumn(
        actions=('delete', 'changelog'),
        extra_buttons="""
            <button type="submit"
                    formaction="{% url 'plugins:netbox_windows_dhcp:dhcpfailover_toggle_sync' record.pk %}"
                    class="btn btn-sm {% if record.sync_enabled %}btn-success{% else %}btn-secondary{% endif %}"
                    title="Toggle Sync">
              <i class="mdi mdi-sync{% if not record.sync_enabled %}-off{% endif %}"></i>
            </button>
        """,
    )

    class Meta(NetBoxTable.Meta):
        model = DHCPFailover
        fields = (
            'pk', 'name', 'primary_server', 'secondary_server',
            'mode', 'max_client_lead_time', 'max_response_delay', 'enable_auth', 'sync_enabled', 'actions',
        )
        default_columns = (
            'name', 'primary_server', 'secondary_server', 'mode', 'sync_enabled', 'actions',
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
        fields = ('pk', 'start_ip', 'end_ip', 'scope', 'actions')
        default_columns = ('start_ip', 'end_ip', 'actions')


class DHCPScopeTable(NetBoxTable):
    name = tables.Column(linkify=True)
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
            'pk', 'name', 'prefix', 'start_ip', 'end_ip',
            'router', 'source', 'lease_lifetime', 'tags', 'actions',
        )
        default_columns = (
            'name', 'prefix', 'start_ip', 'end_ip', 'source', 'tags', 'actions',
        )
