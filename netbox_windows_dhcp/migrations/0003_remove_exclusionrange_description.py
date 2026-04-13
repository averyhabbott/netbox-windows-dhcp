from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0002_populate_option_codes'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='dhcpexclusionrange',
            name='description',
        ),
    ]
