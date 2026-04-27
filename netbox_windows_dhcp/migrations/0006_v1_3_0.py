import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0005_dhcpserver_ca_cert'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── DHCPPluginSettings ──────────────────────────────────────────────
        migrations.AddField(
            model_name='dhcppluginsettings',
            name='api_enabled',
            field=models.BooleanField(
                default=True,
                verbose_name='API Enabled',
                help_text='When disabled, all plugin REST API endpoints return 503 Service Unavailable.',
            ),
        ),

        # ── DHCPServer — maintenance ────────────────────────────────────────
        migrations.AddField(
            model_name='dhcpserver',
            name='maintenance_mode',
            field=models.BooleanField(default=False, verbose_name='Maintenance Mode'),
        ),
        migrations.AddField(
            model_name='dhcpserver',
            name='maintenance_enabled_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='dhcpserver',
            name='maintenance_enabled_by',
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='dhcpserver',
            name='maintenance_notes',
            field=models.TextField(blank=True, default=''),
        ),

        # ── DHCPServer — health tracking ────────────────────────────────────
        migrations.AddField(
            model_name='dhcpserver',
            name='health_status',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('unknown', 'Unknown'),
                    ('healthy', 'Healthy'),
                    ('unreachable', 'Unreachable'),
                ],
                default='unknown',
                verbose_name='Health Status',
            ),
        ),
        migrations.AddField(
            model_name='dhcpserver',
            name='last_health_check',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='dhcpserver',
            name='health_error',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='dhcpserver',
            name='last_sync_at',
            field=models.DateTimeField(null=True, blank=True, verbose_name='Last Sync'),
        ),
        migrations.AddField(
            model_name='dhcpserver',
            name='last_sync_error',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='dhcpserver',
            name='psu_script_version',
            field=models.CharField(max_length=50, blank=True, default='', verbose_name='PSU Script Version'),
        ),

        # ── DHCPFailover — maintenance ──────────────────────────────────────
        migrations.AddField(
            model_name='dhcpfailover',
            name='maintenance_mode',
            field=models.BooleanField(default=False, verbose_name='Maintenance Mode'),
        ),
        migrations.AddField(
            model_name='dhcpfailover',
            name='maintenance_enabled_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='dhcpfailover',
            name='maintenance_enabled_by',
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='dhcpfailover',
            name='maintenance_notes',
            field=models.TextField(blank=True, default=''),
        ),

        # ── DHCPScope — maintenance + sync tracking ─────────────────────────
        migrations.AddField(
            model_name='dhcpscope',
            name='maintenance_mode',
            field=models.BooleanField(default=False, verbose_name='Maintenance Mode'),
        ),
        migrations.AddField(
            model_name='dhcpscope',
            name='maintenance_enabled_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='dhcpscope',
            name='maintenance_enabled_by',
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='dhcpscope',
            name='maintenance_notes',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='dhcpscope',
            name='last_sync_at',
            field=models.DateTimeField(null=True, blank=True, verbose_name='Last Sync'),
        ),
    ]
