import django_tables2 as tables
from netbox.tables import NetBoxTable, BooleanColumn, ActionsColumn, TagColumn

from .models import (
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
        fields = ('pk', 'name', 'hostname', 'port', 'use_https', 'has_api_key', 'actions')
        default_columns = ('name', 'hostname', 'port', 'use_https', 'actions')

    def render_has_api_key(self, value):
        return 'Yes' if value else 'No'


class DHCPFailoverTable(NetBoxTable):
    name = tables.Column(linkify=True)
    primary_server = tables.Column(linkify=True)
    secondary_server = tables.Column(linkify=True)
    mode = tables.Column()
    enable_auth = BooleanColumn(verbose_name='Auth')

    class Meta(NetBoxTable.Meta):
        model = DHCPFailover
        fields = (
            'pk', 'name', 'primary_server', 'secondary_server',
            'mode', 'max_client_lead_time', 'max_response_delay', 'enable_auth', 'actions',
        )
        default_columns = (
            'name', 'primary_server', 'secondary_server', 'mode', 'enable_auth', 'actions',
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


class DHCPScopeTable(NetBoxTable):
    name = tables.Column(linkify=True)
    prefix = tables.Column(linkify=True)
    start_ip = tables.Column(verbose_name='Start IP')
    end_ip = tables.Column(verbose_name='End IP')
    router = tables.Column(verbose_name='Router')
    failover = tables.Column(linkify=True)
    lease_lifetime = tables.Column(verbose_name='Lease Life (s)')
    tags = TagColumn(url_name='plugins:netbox_windows_dhcp:dhcpscope_list')

    class Meta(NetBoxTable.Meta):
        model = DHCPScope
        fields = (
            'pk', 'name', 'prefix', 'start_ip', 'end_ip',
            'router', 'failover', 'lease_lifetime', 'tags', 'actions',
        )
        default_columns = (
            'name', 'prefix', 'start_ip', 'end_ip', 'failover', 'tags', 'actions',
        )
