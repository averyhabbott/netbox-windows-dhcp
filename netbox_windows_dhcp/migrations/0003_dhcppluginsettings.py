from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0002_populate_option_codes'),
    ]

    operations = [
        migrations.CreateModel(
            name='DHCPPluginSettings',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('scope_sync_mode', models.CharField(
                    choices=[
                        ('passive', 'Passive — sync scope info, do not update IP Addresses'),
                        ('active', 'Active — update NetBox IP Addresses with lease/reservation data'),
                    ],
                    default='passive',
                    help_text=(
                        'Active: pull leases/reservations from DHCP servers and update '
                        'NetBox IP Address status. Passive: sync scope config only.'
                    ),
                    max_length=10,
                    verbose_name='Scope Data Sync Mode',
                )),
                ('push_reservations', models.BooleanField(
                    default=False,
                    help_text=(
                        'When enabled, NetBox IP Addresses with status "reserved" are '
                        'pushed to the DHCP server as reservations.'
                    ),
                    verbose_name='Push Reservations to DHCP Server',
                )),
                ('push_scope_info', models.BooleanField(
                    default=False,
                    help_text=(
                        'When enabled, scope configuration (name, range, options) is '
                        'pushed from NetBox to the DHCP server on save.'
                    ),
                    verbose_name='Push Scope Info to DHCP Server',
                )),
                ('sync_interval', models.PositiveIntegerField(
                    default=60,
                    help_text='How often the background sync job runs (5–1440 minutes).',
                    verbose_name='Sync Interval (minutes)',
                )),
            ],
            options={
                'verbose_name': 'Plugin Settings',
            },
        ),
    ]
