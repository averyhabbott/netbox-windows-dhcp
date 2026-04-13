from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0005_sync_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='dhcpscope',
            name='server',
            field=models.ForeignKey(
                blank=True,
                help_text='For standalone scopes not part of a failover relationship.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='standalone_scopes',
                to='netbox_windows_dhcp.dhcpserver',
                verbose_name='Server',
            ),
        ),
    ]
