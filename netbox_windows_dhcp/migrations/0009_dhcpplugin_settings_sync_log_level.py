from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0008_dhcpplugin_settings_sync_job_timeout'),
    ]

    operations = [
        migrations.AddField(
            model_name='dhcppluginsettings',
            name='sync_log_level',
            field=models.CharField(
                choices=[
                    ('DEBUG', 'Debug'),
                    ('INFO', 'Info'),
                    ('WARNING', 'Warning'),
                    ('ERROR', 'Error'),
                ],
                default='DEBUG',
                help_text=(
                    'Minimum severity written to the job log for DHCP sync/push/delete jobs. '
                    'Lower levels produce more detail but slow down large syncs. '
                    'Does not affect the Import or Update PSU Scripts jobs.'
                ),
                max_length=10,
                verbose_name='Sync Job Log Level',
            ),
        ),
        migrations.AddField(
            model_name='dhcpserver',
            name='access_level',
            field=models.CharField(
                choices=[
                    ('unknown', 'Unknown'),
                    ('ro', 'Read-Only'),
                    ('rw', 'Read-Write'),
                ],
                default='unknown',
                max_length=20,
                verbose_name='Access Level',
            ),
        ),
    ]
