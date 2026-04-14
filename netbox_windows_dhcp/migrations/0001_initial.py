import django.db.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('extras', '0001_initial'),
        ('ipam', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='DHCPPluginSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('sync_ip_addresses', models.BooleanField(
                    default=False,
                    verbose_name='Sync IP Addresses from Leases & Reservations',
                    help_text=(
                        'When enabled, pull leases and reservations from DHCP servers and '
                        'create/update/delete NetBox IP Address records with status, DNS name, '
                        'and client MAC.'
                    ),
                )),
                ('push_reservations', models.BooleanField(
                    default=False,
                    verbose_name='Push Reservations to DHCP Server',
                    help_text=(
                        'When enabled, NetBox IP Addresses with status "reserved" are '
                        'pushed to the DHCP server as reservations.'
                    ),
                )),
                ('push_scope_info', models.BooleanField(
                    default=False,
                    verbose_name='Push Scope Info to DHCP Server',
                    help_text=(
                        'When enabled, scope configuration (name, range, options) is '
                        'pushed from NetBox to the DHCP server on save.'
                    ),
                )),
                ('sync_interval', models.PositiveIntegerField(
                    default=60,
                    verbose_name='Sync Interval (minutes)',
                    help_text='How often the background sync job runs (5–1440 minutes).',
                )),
                ('sync_queue', models.CharField(
                    choices=[('high', 'High'), ('default', 'Default'), ('low', 'Low')],
                    default='default',
                    max_length=20,
                    verbose_name='Sync Job Queue',
                    help_text='Worker queue priority used for all DHCP sync and import jobs.',
                )),
                ('sync_protect_tag', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to='extras.tag',
                    verbose_name='Sync-Protected Tag',
                    help_text=(
                        'IP Addresses carrying this tag are fully protected from sync: '
                        'status, DNS name, and the IP itself are never modified or removed by the sync. '
                        'Leave blank to disable.'
                    ),
                )),
                ('sync_protect_update_client_id', models.BooleanField(
                    default=False,
                    verbose_name='Update Client ID for Protected IPs',
                    help_text=(
                        "When enabled, the sync updates the DHCP Client ID field on protected IPs to match "
                        "the DHCP server's active lease (useful after a server replacement when the client "
                        "MAC changes). All other sync writes are still blocked for protected IPs."
                    ),
                )),
            ],
            options={
                'verbose_name': 'Plugin Settings',
            },
        ),
        migrations.CreateModel(
            name='DHCPServer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('hostname', models.CharField(max_length=255, help_text='Hostname or IP address of the server')),
                ('port', models.PositiveIntegerField(default=443)),
                ('use_https', models.BooleanField(default=True, verbose_name='Use HTTPS')),
                ('api_key', models.CharField(
                    max_length=2000,
                    blank=True,
                    verbose_name='App Token',
                    help_text=(
                        'PSU v5 App Token (Security \u2192 App Tokens in the PSU admin console). '
                        'Sent as Authorization: Bearer. Leave blank if auth is not required.'
                    ),
                )),
                ('verify_ssl', models.BooleanField(
                    default=True,
                    verbose_name='Verify SSL Certificate',
                    help_text='Uncheck to disable TLS certificate verification (for self-signed certs in test environments).',
                )),
                ('sync_standalone_scopes', models.BooleanField(
                    default=True,
                    verbose_name='Sync Standalone Scopes',
                    help_text=(
                        'When enabled, scopes with no failover relationship are included '
                        'in sync operations for this server.'
                    ),
                )),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'DHCP Server',
                'verbose_name_plural': 'DHCP Servers',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='DHCPOptionCodeDefinition',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('code', models.PositiveSmallIntegerField(
                    unique=True,
                    verbose_name='Option Code',
                    help_text='DHCP option code number (1\u2013254)',
                )),
                ('name', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True)),
                ('data_type', models.CharField(
                    max_length=20,
                    choices=[
                        ('String', 'String'),
                        ('IPAddress', 'IP Address'),
                        ('IPAddressList', 'IP Address List'),
                        ('DWORD', 'DWORD (32-bit)'),
                        ('DWORD DWORD', 'DWORD DWORD (64-bit)'),
                        ('Binary', 'Binary'),
                        ('Encapsulated', 'Encapsulated'),
                        ('IPv6Address', 'IPv6 Address'),
                    ],
                    default='String',
                )),
                ('is_builtin', models.BooleanField(
                    default=False,
                    verbose_name='Built-in',
                    help_text='Built-in Windows DHCP option \u2014 deletion is restricted',
                )),
                ('vendor_class', models.CharField(
                    max_length=200,
                    blank=True,
                    verbose_name='Vendor Class',
                    help_text='Leave blank for standard options',
                )),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'DHCP Option Code Definition',
                'verbose_name_plural': 'DHCP Option Code Definitions',
                'ordering': ['code'],
            },
        ),
        migrations.CreateModel(
            name='DHCPFailover',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('mode', models.CharField(
                    max_length=20,
                    choices=[('LoadBalance', 'Load Balance'), ('HotStandby', 'Hot Standby')],
                    default='LoadBalance',
                )),
                ('max_client_lead_time', models.PositiveIntegerField(
                    default=3600,
                    verbose_name='Max Client Lead Time (s)',
                    help_text='Seconds',
                )),
                ('max_response_delay', models.PositiveIntegerField(
                    default=30,
                    verbose_name='Max Response Delay (s)',
                    help_text='Seconds',
                )),
                ('state_switchover_interval', models.PositiveIntegerField(
                    null=True,
                    blank=True,
                    verbose_name='State Switchover Interval (s)',
                    help_text='Seconds. Leave blank to disable automatic switchover.',
                )),
                ('sync_enabled', models.BooleanField(
                    default=True,
                    verbose_name='Sync Enabled',
                    help_text=(
                        'When enabled, scopes using this failover relationship are included '
                        'in sync operations.'
                    ),
                )),
                ('enable_auth', models.BooleanField(
                    default=False,
                    verbose_name='Enable Authentication',
                )),
                ('shared_secret', models.CharField(
                    max_length=500,
                    blank=True,
                    verbose_name='Shared Secret',
                    help_text='Required when authentication is enabled',
                )),
                ('primary_server', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='primary_failovers',
                    to='netbox_windows_dhcp.dhcpserver',
                    verbose_name='Primary Server',
                )),
                ('secondary_server', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='secondary_failovers',
                    to='netbox_windows_dhcp.dhcpserver',
                    verbose_name='Secondary Server',
                )),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'DHCP Failover',
                'verbose_name_plural': 'DHCP Failovers',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='DHCPOptionValue',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('value', models.TextField(help_text='The option value (e.g. IP address, string, hex bytes)')),
                ('friendly_name', models.CharField(
                    max_length=200,
                    blank=True,
                    verbose_name='Friendly Name',
                    help_text='Optional human-readable label. If blank, displays as "<code>: <value>".',
                )),
                ('option_definition', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='values',
                    to='netbox_windows_dhcp.dhcpoptioncodedefinition',
                    verbose_name='Option Definition',
                )),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'DHCP Option Value',
                'verbose_name_plural': 'DHCP Option Values',
                'ordering': ['option_definition__code', 'friendly_name', 'value'],
            },
        ),
        migrations.CreateModel(
            name='DHCPScope',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('name', models.CharField(max_length=200)),
                ('start_ip', models.GenericIPAddressField(verbose_name='Start IP')),
                ('end_ip', models.GenericIPAddressField(verbose_name='End IP')),
                ('router', models.GenericIPAddressField(
                    null=True,
                    blank=True,
                    verbose_name='Router (Option 3)',
                    help_text='Default gateway IP address for this scope',
                )),
                ('lease_lifetime', models.PositiveIntegerField(
                    default=86400,
                    verbose_name='Lease Lifetime',
                    help_text='Lease duration in seconds',
                )),
                ('prefix', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='dhcp_scopes',
                    to='ipam.prefix',
                    verbose_name='Prefix',
                )),
                ('server', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='standalone_scopes',
                    to='netbox_windows_dhcp.dhcpserver',
                    verbose_name='Server',
                    help_text='For standalone scopes not part of a failover relationship.',
                )),
                ('failover', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='scopes',
                    to='netbox_windows_dhcp.dhcpfailover',
                    verbose_name='Failover Relationship',
                )),
                ('option_values', models.ManyToManyField(
                    blank=True,
                    related_name='scopes',
                    to='netbox_windows_dhcp.dhcpoptionvalue',
                    verbose_name='Option Values',
                )),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'DHCP Scope',
                'verbose_name_plural': 'DHCP Scopes',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='DHCPExclusionRange',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('start_ip', models.GenericIPAddressField(verbose_name='Start IP')),
                ('end_ip', models.GenericIPAddressField(verbose_name='End IP')),
                ('scope', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='exclusion_ranges',
                    to='netbox_windows_dhcp.dhcpscope',
                    verbose_name='Scope',
                )),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'DHCP Exclusion Range',
                'verbose_name_plural': 'DHCP Exclusion Ranges',
                'ordering': ['scope', 'start_ip'],
                'unique_together': {('scope', 'start_ip', 'end_ip')},
            },
        ),
        migrations.CreateModel(
            name='DHCPLeaseInfo',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('lease_hostname', models.CharField(
                    blank=True,
                    default='',
                    max_length=255,
                    verbose_name='Lease Hostname',
                    help_text='Hostname reported by the DHCP server for this lease or reservation.',
                )),
                ('active', models.BooleanField(
                    default=False,
                    verbose_name='Active',
                    help_text='True if this IP was seen as an active lease or reservation on the last sync.',
                )),
                ('lease_expiration', models.DateTimeField(
                    blank=True,
                    null=True,
                    verbose_name='Lease Expiration',
                    help_text='When this lease expires. Null for reservations (they do not expire).',
                )),
                ('ip_address', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='dhcp_lease_info',
                    to='ipam.ipaddress',
                )),
            ],
            options={
                'verbose_name': 'DHCP Lease Info',
                'verbose_name_plural': 'DHCP Lease Info',
            },
        ),
    ]
