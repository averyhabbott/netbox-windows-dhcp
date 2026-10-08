from django.db import migrations, models
from django.utils import timezone


def start_clocks_now(apps, schema_editor):
    """Windows keeps no history, so every existing row's clock starts at the upgrade."""
    DHCPLeaseInfo = apps.get_model('netbox_windows_dhcp', 'DHCPLeaseInfo')
    DHCPLeaseInfo.objects.filter(state_changed__isnull=True).update(state_changed=timezone.now())


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0010_descriptions_and_reservation_placeholders'),
    ]

    operations = [
        migrations.AddField(
            model_name='dhcpleaseinfo',
            name='state_changed',
            field=models.DateTimeField(
                blank=True, null=True, verbose_name='Active/Inactive Since',
                help_text=(
                    'When the current state began: set when the row is first recorded, and reset '
                    "whenever Active flips or the IP's DHCP client ID changes (whether the sync "
                    'or someone in NetBox changed it). Precision is the sync interval.'
                ),
            ),
        ),
        migrations.RunPython(start_clocks_now, migrations.RunPython.noop),
    ]
