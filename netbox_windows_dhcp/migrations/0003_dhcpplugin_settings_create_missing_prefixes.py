from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0002_populate_option_codes'),
    ]

    operations = [
        migrations.AddField(
            model_name='dhcppluginsettings',
            name='create_missing_prefixes',
            field=models.BooleanField(
                default=True,
                help_text=(
                    'When enabled, importing a scope whose CIDR does not exist in NetBox will '
                    'automatically create the Prefix. Disable if Prefixes are managed by another '
                    'source and should never be created by the DHCP plugin.'
                ),
                verbose_name='Create Missing Prefixes on Import',
            ),
        ),
    ]
