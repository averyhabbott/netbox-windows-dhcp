import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('extras', '0001_initial'),
        ('ipam', '0001_initial'),
    ]

    # We rely on netbox's migration dependency resolution — use a broad dep pattern
    # that covers NetBox 4.5+ extras/ipam initial migrations.
    # In practice NetBox migrations are numbered; adjust if needed.

    operations = [
        migrations.CreateModel(
            name='DHCPServer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=None)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('hostname', models.CharField(max_length=255)),
                ('port', models.PositiveIntegerField(default=443)),
                ('use_https', models.BooleanField(default=True)),
                ('api_key', models.CharField(blank=True, max_length=500)),
                ('tags', models.ManyToManyField(blank=True, to='extras.tag')),
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
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=None)),
                ('code', models.PositiveSmallIntegerField(unique=True)),
                ('name', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True)),
                ('data_type', models.CharField(
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
                    max_length=20,
                )),
                ('is_builtin', models.BooleanField(default=False)),
                ('vendor_class', models.CharField(blank=True, max_length=200)),
                ('tags', models.ManyToManyField(blank=True, to='extras.tag')),
            ],
            options={
                'verbose_name': 'DHCP Option Code Definition',
                'verbose_name_plural': 'DHCP Option Code Definitions',
                'ordering': ['code'],
            },
        ),
        migrations.CreateModel(
            name='DHCPOptionValue',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=None)),
                ('value', models.TextField()),
                ('friendly_name', models.CharField(blank=True, max_length=200)),
                ('option_definition', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='values',
                    to='netbox_windows_dhcp.dhcpoptioncodedefinition',
                )),
                ('tags', models.ManyToManyField(blank=True, to='extras.tag')),
            ],
            options={
                'verbose_name': 'DHCP Option Value',
                'verbose_name_plural': 'DHCP Option Values',
                'ordering': ['option_definition__code', 'friendly_name', 'value'],
            },
        ),
        migrations.CreateModel(
            name='DHCPFailover',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=None)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('mode', models.CharField(
                    choices=[('LoadBalance', 'Load Balance'), ('HotStandby', 'Hot Standby')],
                    default='LoadBalance',
                    max_length=20,
                )),
                ('max_client_lead_time', models.PositiveIntegerField(default=3600)),
                ('max_response_delay', models.PositiveIntegerField(default=30)),
                ('state_switchover_interval', models.PositiveIntegerField(blank=True, null=True)),
                ('enable_auth', models.BooleanField(default=False)),
                ('shared_secret', models.CharField(blank=True, max_length=500)),
                ('primary_server', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='primary_failovers',
                    to='netbox_windows_dhcp.dhcpserver',
                )),
                ('secondary_server', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='secondary_failovers',
                    to='netbox_windows_dhcp.dhcpserver',
                )),
                ('tags', models.ManyToManyField(blank=True, to='extras.tag')),
            ],
            options={
                'verbose_name': 'DHCP Failover',
                'verbose_name_plural': 'DHCP Failovers',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='DHCPScope',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=None)),
                ('name', models.CharField(max_length=200)),
                ('start_ip', models.GenericIPAddressField()),
                ('end_ip', models.GenericIPAddressField()),
                ('router', models.GenericIPAddressField(blank=True, null=True)),
                ('lease_lifetime', models.PositiveIntegerField(default=86400)),
                ('prefix', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='dhcp_scopes',
                    to='ipam.prefix',
                )),
                ('failover', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='scopes',
                    to='netbox_windows_dhcp.dhcpfailover',
                )),
                ('option_values', models.ManyToManyField(
                    blank=True,
                    related_name='scopes',
                    to='netbox_windows_dhcp.dhcpoptionvalue',
                )),
                ('tags', models.ManyToManyField(blank=True, to='extras.tag')),
            ],
            options={
                'verbose_name': 'DHCP Scope',
                'verbose_name_plural': 'DHCP Scopes',
                'ordering': ['name'],
            },
        ),
    ]
