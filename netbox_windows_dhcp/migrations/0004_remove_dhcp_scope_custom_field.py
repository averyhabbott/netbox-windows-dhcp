from django.db import migrations


def remove_dhcp_scope_custom_field(apps, schema_editor):
    """Delete the dhcp_scope custom field if it exists."""
    try:
        CustomField = apps.get_model('extras', 'CustomField')
        CustomField.objects.filter(name='dhcp_scope').delete()
    except Exception:
        pass


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0003_remove_exclusionrange_description'),
    ]

    operations = [
        migrations.RunPython(remove_dhcp_scope_custom_field, migrations.RunPython.noop),
    ]
