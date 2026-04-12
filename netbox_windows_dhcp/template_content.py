from netbox.plugins.templates import PluginTemplateExtension


class PrefixDHCPScopesPanel(PluginTemplateExtension):
    """Injects a 'DHCP Scopes' panel into the Prefix detail view."""

    models = ['ipam.prefix']

    def right_page(self):
        prefix = self.context['object']
        scopes = prefix.dhcp_scopes.select_related('failover').prefetch_related(
            'option_values__option_definition'
        ).order_by('name')
        return self.render(
            'netbox_windows_dhcp/prefix_dhcp_panel.html',
            extra_context={'dhcp_scopes': scopes},
        )


template_extensions = (PrefixDHCPScopesPanel,)
