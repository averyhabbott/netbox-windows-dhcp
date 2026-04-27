from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0006_v1_3_0'),
    ]

    operations = [
        migrations.AddField(
            model_name='dhcppluginsettings',
            name='sync_active_scopes_only',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'When enabled, disabled/inactive scopes on the Windows DHCP server are ignored '
                    'during sync. Useful when migrating scopes between servers — disable the scope '
                    'on the old server and activate it on the new one to control which server NetBox '
                    'treats as authoritative.'
                ),
                verbose_name='Sync Active Scopes Only',
            ),
        ),
    ]
