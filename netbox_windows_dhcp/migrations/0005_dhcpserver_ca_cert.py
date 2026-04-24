from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0004_dhcpplugin_settings_configurable_statuses'),
    ]

    operations = [
        migrations.AddField(
            model_name='dhcpserver',
            name='ca_cert',
            field=models.TextField(
                blank=True,
                default='',
                verbose_name='Stored CA Certificate',
                help_text=(
                    'PEM-encoded CA certificate imported via "Import HTTPS Certificate". '
                    'Used for TLS verification when Verify SSL is enabled.'
                ),
            ),
        ),
        migrations.AddField(
            model_name='dhcpserver',
            name='ca_cert_expiry',
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name='CA Certificate Expiry',
            ),
        ),
    ]
