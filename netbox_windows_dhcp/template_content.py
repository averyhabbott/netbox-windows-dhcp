from netbox.plugins.templates import PluginTemplateExtension


class PrefixDHCPScopesPanel(PluginTemplateExtension):
    """Injects a 'DHCP Scopes' panel into the Prefix detail view."""

    models = ['ipam.prefix']

    def left_page(self):
        prefix = self.context['object']
        scopes = prefix.dhcp_scopes.select_related('failover').prefetch_related(
            'option_values__option_definition'
        ).order_by('name')
        return self.render(
            'netbox_windows_dhcp/prefix_dhcp_panel.html',
            extra_context={'dhcp_scopes': scopes},
        )


class IPAddressDHCPPanel(PluginTemplateExtension):
    """
    Injects a 'DHCP Lease Details' panel into the IP Address detail view.
    Only rendered when the IP has a DHCPLeaseInfo record (i.e. was written by the sync).
    """

    models = ['ipam.ipaddress']

    def right_page(self):
        ip_obj = self.context['object']
        try:
            dhcp_info = ip_obj.dhcp_lease_info
        except Exception:
            return ''

        return self.render(
            'netbox_windows_dhcp/inc/ipaddress_dhcp_panel.html',
            extra_context={'dhcp_info': dhcp_info},
        )


template_extensions = (PrefixDHCPScopesPanel, IPAddressDHCPPanel)
