"""
Sync migration state with current models.py.

All operations are metadata-only (verbose_name / help_text) and produce no
database schema changes — they simply update Django's migration state so that
`manage.py migrate --check` passes cleanly.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ipam', '0001_initial'),
        ('netbox_windows_dhcp', '0006_dhcppluginsettings_sync_ip_addresses'),
    ]

    operations = [
        # --- DHCPServer ---
        migrations.AlterField(
            model_name='dhcpserver',
            name='hostname',
            field=models.CharField(
                max_length=255,
                help_text='Hostname or IP address of the server',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpserver',
            name='use_https',
            field=models.BooleanField(
                default=True,
                verbose_name='Use HTTPS',
            ),
        ),
        # --- DHCPFailover ---
        migrations.AlterField(
            model_name='dhcpfailover',
            name='primary_server',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='primary_failovers',
                to='netbox_windows_dhcp.dhcpserver',
                verbose_name='Primary Server',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpfailover',
            name='secondary_server',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='secondary_failovers',
                to='netbox_windows_dhcp.dhcpserver',
                verbose_name='Secondary Server',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpfailover',
            name='max_client_lead_time',
            field=models.PositiveIntegerField(
                default=3600,
                verbose_name='Max Client Lead Time (s)',
                help_text='Seconds',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpfailover',
            name='max_response_delay',
            field=models.PositiveIntegerField(
                default=30,
                verbose_name='Max Response Delay (s)',
                help_text='Seconds',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpfailover',
            name='state_switchover_interval',
            field=models.PositiveIntegerField(
                null=True,
                blank=True,
                verbose_name='State Switchover Interval (s)',
                help_text='Seconds. Leave blank to disable automatic switchover.',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpfailover',
            name='enable_auth',
            field=models.BooleanField(
                default=False,
                verbose_name='Enable Authentication',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpfailover',
            name='shared_secret',
            field=models.CharField(
                max_length=500,
                blank=True,
                verbose_name='Shared Secret',
                help_text='Required when authentication is enabled',
            ),
        ),
        # --- DHCPOptionCodeDefinition ---
        migrations.AlterField(
            model_name='dhcpoptioncodedefinition',
            name='code',
            field=models.PositiveSmallIntegerField(
                unique=True,
                verbose_name='Option Code',
                help_text='DHCP option code number (1–254)',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpoptioncodedefinition',
            name='is_builtin',
            field=models.BooleanField(
                default=False,
                verbose_name='Built-in',
                help_text='Built-in Windows DHCP option — deletion is restricted',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpoptioncodedefinition',
            name='vendor_class',
            field=models.CharField(
                max_length=200,
                blank=True,
                verbose_name='Vendor Class',
                help_text='Leave blank for standard options',
            ),
        ),
        # --- DHCPOptionValue ---
        migrations.AlterField(
            model_name='dhcpoptionvalue',
            name='value',
            field=models.TextField(
                help_text='The option value (e.g. IP address, string, hex bytes)',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpoptionvalue',
            name='friendly_name',
            field=models.CharField(
                max_length=200,
                blank=True,
                verbose_name='Friendly Name',
                help_text='Optional human-readable label. If blank, displays as "<code>: <value>".',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpoptionvalue',
            name='option_definition',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='values',
                to='netbox_windows_dhcp.dhcpoptioncodedefinition',
                verbose_name='Option Definition',
            ),
        ),
        # --- DHCPScope ---
        migrations.AlterField(
            model_name='dhcpscope',
            name='prefix',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='dhcp_scopes',
                to='ipam.prefix',
                verbose_name='Prefix',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpscope',
            name='start_ip',
            field=models.GenericIPAddressField(
                verbose_name='Start IP',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpscope',
            name='end_ip',
            field=models.GenericIPAddressField(
                verbose_name='End IP',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpscope',
            name='router',
            field=models.GenericIPAddressField(
                null=True,
                blank=True,
                verbose_name='Router (Option 3)',
                help_text='Default gateway IP address for this scope',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpscope',
            name='lease_lifetime',
            field=models.PositiveIntegerField(
                default=86400,
                verbose_name='Lease Lifetime',
                help_text='Lease duration in seconds',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpscope',
            name='failover',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='scopes',
                to='netbox_windows_dhcp.dhcpfailover',
                verbose_name='Failover Relationship',
            ),
        ),
        migrations.AlterField(
            model_name='dhcpscope',
            name='option_values',
            field=models.ManyToManyField(
                blank=True,
                related_name='scopes',
                to='netbox_windows_dhcp.dhcpoptionvalue',
                verbose_name='Option Values',
            ),
        ),
    ]
