from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0004_dhcpserver_verify_ssl'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dhcpserver',
            name='api_key',
            field=models.CharField(
                max_length=2000,
                blank=True,
                verbose_name='App Token',
                help_text='PSU v5 App Token (Security \u2192 App Tokens in the PSU admin console). Sent as Authorization: Bearer. Leave blank if auth is not required.',
            ),
        ),
    ]
