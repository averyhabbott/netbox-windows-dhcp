from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0004_remove_dhcp_scope_custom_field'),
    ]

    operations = [
        migrations.AddField(
            model_name='dhcpserver',
            name='sync_standalone_scopes',
            field=models.BooleanField(
                default=True,
                verbose_name='Sync Standalone Scopes',
                help_text=(
                    'When enabled, scopes with no failover relationship are included '
                    'in sync operations for this server.'
                ),
            ),
        ),
        migrations.AddField(
            model_name='dhcpfailover',
            name='sync_enabled',
            field=models.BooleanField(
                default=True,
                verbose_name='Sync Enabled',
                help_text=(
                    'When enabled, scopes using this failover relationship are included '
                    'in sync operations.'
                ),
            ),
        ),
    ]
