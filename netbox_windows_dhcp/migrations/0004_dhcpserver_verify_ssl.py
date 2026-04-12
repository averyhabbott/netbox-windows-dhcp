from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0003_dhcppluginsettings'),
    ]

    operations = [
        migrations.AddField(
            model_name='dhcpserver',
            name='verify_ssl',
            field=models.BooleanField(
                default=True,
                verbose_name='Verify SSL Certificate',
                help_text='Uncheck to disable TLS certificate verification (for self-signed certs in test environments).',
            ),
        ),
    ]
