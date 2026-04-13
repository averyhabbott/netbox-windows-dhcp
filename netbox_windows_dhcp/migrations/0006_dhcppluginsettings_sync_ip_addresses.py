from django.db import migrations, models


def migrate_sync_mode(apps, schema_editor):
    """Convert scope_sync_mode='active' → sync_ip_addresses=True."""
    DHCPPluginSettings = apps.get_model('netbox_windows_dhcp', 'DHCPPluginSettings')
    DHCPPluginSettings.objects.filter(scope_sync_mode='active').update(sync_ip_addresses=True)


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0005_dhcpserver_api_key_max_length'),
    ]

    operations = [
        migrations.AddField(
            model_name='dhcppluginsettings',
            name='sync_ip_addresses',
            field=models.BooleanField(
                default=False,
                verbose_name='Sync IP Addresses from Leases & Reservations',
                help_text=(
                    'When enabled, pull leases and reservations from DHCP servers and '
                    'create/update/delete NetBox IP Address records with status, DNS name, '
                    'and client MAC.'
                ),
            ),
        ),
        migrations.RunPython(migrate_sync_mode, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='dhcppluginsettings',
            name='scope_sync_mode',
        ),
    ]
