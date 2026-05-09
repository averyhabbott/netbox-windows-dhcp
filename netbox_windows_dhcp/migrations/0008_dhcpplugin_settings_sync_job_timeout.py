from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0007_dhcpplugin_settings_sync_active_scopes_only'),
    ]

    operations = [
        migrations.AddField(
            model_name='dhcppluginsettings',
            name='sync_job_timeout',
            field=models.PositiveIntegerField(
                default=300,
                help_text=(
                    'Maximum wall-clock seconds a sync job may run before RQ kills it. '
                    'Default 300 matches RQ_DEFAULT_TIMEOUT. Increase for servers with '
                    'large scope counts. CONN_MAX_AGE is automatically aligned to this '
                    'value at job start.'
                ),
                verbose_name='Sync Job Timeout (seconds)',
            ),
        ),
    ]
