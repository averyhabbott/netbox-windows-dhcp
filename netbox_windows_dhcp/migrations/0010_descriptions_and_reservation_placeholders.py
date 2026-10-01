import django.db.models.deletion
from django.db import migrations, models


def fill_scope_networks(apps, schema_editor):
    """Copy each scope's network and prefix length from its prefix."""
    from netaddr import IPNetwork

    DHCPScope = apps.get_model('netbox_windows_dhcp', 'DHCPScope')
    for scope in DHCPScope.objects.select_related('prefix'):
        net = IPNetwork(str(scope.prefix.prefix))
        DHCPScope.objects.filter(pk=scope.pk).update(
            network=str(net.network), prefix_length=net.prefixlen,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0009_dhcpplugin_settings_sync_log_level'),
    ]

    operations = [
        migrations.AddField(
            model_name='dhcpscope',
            name='description',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='dhcpscope',
            name='active',
            field=models.BooleanField(
                default=True,
                help_text='Whether the scope is active on the DHCP server.',
                verbose_name='Active',
            ),
        ),
        migrations.AddField(
            model_name='dhcpexclusionrange',
            name='description',
            field=models.CharField(
                blank=True,
                help_text='Stored in NetBox only — Windows DHCP exclusions have no description.',
                max_length=200,
            ),
        ),
        migrations.AddField(
            model_name='dhcppluginsettings',
            name='reservation_placeholders',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Reserving an IP inside a DHCP scope without a client ID does nothing on its own, '
                    'because the DHCP server can still hand that IP out. When enabled (with Push '
                    'Reservations on), such IPs get a placeholder client ID (ba-dc-0d-ed-…) in NetBox '
                    'and on the DHCP server so the server stops handing them out. Turning this off '
                    'stops new placeholders; existing ones are left as they are.'
                ),
                verbose_name='Placeholder Reservations',
            ),
        ),
        # Replaced by the synced DHCPScope.active (phase 21).
        migrations.RemoveField(
            model_name='dhcppluginsettings',
            name='sync_active_scopes_only',
        ),
        migrations.AlterField(
            model_name='dhcppluginsettings',
            name='sync_protect_tag',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to='extras.tag',
                verbose_name='Sync-Protected Tag',
                help_text=(
                    'IP Addresses carrying this tag, or inside a prefix carrying it, are protected from '
                    'being overwritten by a sync: status, DNS name, and the IP itself are never modified '
                    'or removed by the sync. The tag does not stop pushes to the DHCP server. '
                    'Leave blank to disable.'
                ),
            ),
        ),
        migrations.AlterField(
            model_name='dhcppluginsettings',
            name='sync_queue',
            field=models.CharField(
                choices=[('high', 'High'), ('default', 'Default'), ('low', 'Low')],
                default='default',
                max_length=20,
                verbose_name='Sync Job Queue',
                help_text="Worker queue priority used for the plugin's sync, import, push and PSU script update jobs.",
            ),
        ),
        migrations.AlterField(
            model_name='dhcppluginsettings',
            name='push_reservations',
            field=models.BooleanField(
                default=False,
                verbose_name='Push Reservations to DHCP Server',
                help_text=(
                    'When enabled, NetBox is the source of truth for reservations: IP Addresses with the '
                    'configured reservation status are created, updated and deleted on the DHCP server, '
                    'and server reservations NetBox doesn\'t have are deleted. When disabled, the DHCP '
                    'server is the source of truth and NetBox mirrors its reservations. A server with a '
                    'read-only API key always syncs as if this were disabled.'
                ),
            ),
        ),
        migrations.AlterField(
            model_name='dhcppluginsettings',
            name='push_scope_info',
            field=models.BooleanField(
                default=False,
                verbose_name='Push Scope Info to DHCP Server',
                help_text=(
                    'When enabled, NetBox is the source of truth for scopes: name, description, range, '
                    'router, lease time, active state, failover, options and exclusions are pushed to the '
                    'DHCP server, NetBox-only scopes are created on it, and scopes on the server but not '
                    'in NetBox are removed from it. When disabled, the DHCP server is the source of truth: '
                    'scope settings are pulled on every sync, new server scopes are imported, and scopes '
                    'removed from the server are deleted from NetBox. Maintenance mode and the server and '
                    'failover sync settings apply either way. A server with a read-only API key always '
                    'syncs as if this were disabled.'
                ),
            ),
        ),
        migrations.AlterField(
            model_name='dhcppluginsettings',
            name='create_missing_prefixes',
            field=models.BooleanField(
                default=True,
                verbose_name='Create Missing Prefixes on Import',
                help_text=(
                    'When enabled, a scope learned from a DHCP server (by Import or the sync) whose prefix '
                    'isn\'t in NetBox gets one, created in the Default Scope VRF. When disabled, the scope '
                    'is created without a prefix instead (see the Unassigned Scopes saved filter). '
                    'Disable if Prefixes are managed by another source.'
                ),
            ),
        ),
        # ── Scopes without a prefix (phase 15) ─────────────────────────────
        migrations.AlterField(
            model_name='dhcpscope',
            name='prefix',
            field=models.ForeignKey(
                blank=True,
                help_text='Leave blank for a scope with no prefix (IP sync and reservations are skipped for it).',
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='dhcp_scopes',
                to='ipam.prefix',
                verbose_name='Prefix',
            ),
        ),
        migrations.AddField(
            model_name='dhcpscope',
            name='network',
            field=models.GenericIPAddressField(
                blank=True,
                help_text='The scope ID on the DHCP server. Copied from the prefix when one is set.',
                null=True,
                verbose_name='Network',
            ),
        ),
        migrations.AddField(
            model_name='dhcpscope',
            name='prefix_length',
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text='Copied from the prefix when one is set.',
                null=True,
                verbose_name='Prefix Length',
            ),
        ),
        migrations.RunPython(fill_scope_networks, migrations.RunPython.noop),
        # ── VRF and server ties (phase 14) ─────────────────────────────────
        migrations.AddField(
            model_name='dhcpserver',
            name='default_scope_vrf',
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    'Where standalone scopes learned from this server look for (or create) their '
                    'prefix. Blank means the global VRF. Changing it only affects scopes learned '
                    'from then on.'
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to='ipam.vrf',
                verbose_name='Default Scope VRF',
            ),
        ),
        migrations.AddField(
            model_name='dhcpfailover',
            name='default_scope_vrf',
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    'Where scopes learned for this failover look for (or create) their prefix. '
                    'Blank means the global VRF. Changing it only affects scopes learned from then on.'
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to='ipam.vrf',
                verbose_name='Default Scope VRF',
            ),
        ),
        migrations.AddField(
            model_name='dhcpfailover',
            name='description',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AlterField(
            model_name='dhcpfailover',
            name='name',
            field=models.CharField(max_length=100),
        ),
        # Plugin settings get NetBox's change logging (created / last updated)
        migrations.AddField(
            model_name='dhcppluginsettings',
            name='created',
            field=models.DateTimeField(auto_now_add=True, blank=True, null=True, verbose_name='created'),
        ),
        migrations.AddField(
            model_name='dhcppluginsettings',
            name='last_updated',
            field=models.DateTimeField(auto_now=True, blank=True, null=True, verbose_name='last updated'),
        ),
    ]
