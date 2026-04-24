from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0003_dhcpplugin_settings_create_missing_prefixes'),
    ]

    operations = [
        migrations.AddField(
            model_name='dhcppluginsettings',
            name='lease_status',
            field=models.CharField(
                default='dhcp',
                max_length=50,
                verbose_name='DHCP Lease Status',
                help_text=(
                    'IP Address status assigned to active DHCP leases by the sync. '
                    'Changing this mid-deployment will cause the next sync to update all managed IPs to the new status.'
                ),
            ),
        ),
        migrations.AddField(
            model_name='dhcppluginsettings',
            name='reservation_status',
            field=models.CharField(
                default='reserved',
                max_length=50,
                verbose_name='DHCP Reservation Status',
                help_text=(
                    'IP Address status assigned to DHCP reservations by the sync, and the status that '
                    'triggers a push to the DHCP server when Push Reservations is enabled.'
                ),
            ),
        ),
    ]
