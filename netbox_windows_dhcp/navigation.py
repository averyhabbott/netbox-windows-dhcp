from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label='Windows DHCP',
    groups=(
        (
            'Infrastructure',
            (
                PluginMenuItem(
                    link='plugins:netbox_windows_dhcp:dhcpserver_list',
                    link_text='Servers',
                    permissions=['netbox_windows_dhcp.view_dhcpserver'],
                    buttons=(
                        PluginMenuButton(
                            link='plugins:netbox_windows_dhcp:dhcpserver_add',
                            title='Add Server',
                            icon_class='mdi mdi-plus-thick',
                        ),
                    ),
                ),
                PluginMenuItem(
                    link='plugins:netbox_windows_dhcp:dhcpfailover_list',
                    link_text='Failover',
                    permissions=['netbox_windows_dhcp.view_dhcpfailover'],
                    buttons=(
                        PluginMenuButton(
                            link='plugins:netbox_windows_dhcp:dhcpfailover_add',
                            title='Add Failover',
                            icon_class='mdi mdi-plus-thick',
                        ),
                    ),
                ),
            ),
        ),
        (
            'Scopes',
            (
                PluginMenuItem(
                    link='plugins:netbox_windows_dhcp:dhcpscope_list',
                    link_text='Scopes',
                    permissions=['netbox_windows_dhcp.view_dhcpscope'],
                    buttons=(
                        PluginMenuButton(
                            link='plugins:netbox_windows_dhcp:dhcpscope_add',
                            title='Add Scope',
                            icon_class='mdi mdi-plus-thick',
                        ),
                    ),
                ),
            ),
        ),
        (
            'Options',
            (
                PluginMenuItem(
                    link='plugins:netbox_windows_dhcp:dhcpoptionvalue_list',
                    link_text='Option Values',
                    permissions=['netbox_windows_dhcp.view_dhcpoptionvalue'],
                    buttons=(
                        PluginMenuButton(
                            link='plugins:netbox_windows_dhcp:dhcpoptionvalue_add',
                            title='Add Option Value',
                            icon_class='mdi mdi-plus-thick',
                        ),
                    ),
                ),
                PluginMenuItem(
                    link='plugins:netbox_windows_dhcp:dhcpoptioncodedefinition_list',
                    link_text='Option Code Definitions',
                    permissions=['netbox_windows_dhcp.view_dhcpoptioncodedefinition'],
                    buttons=(
                        PluginMenuButton(
                            link='plugins:netbox_windows_dhcp:dhcpoptioncodedefinition_add',
                            title='Add Option Code Definition',
                            icon_class='mdi mdi-plus-thick',
                        ),
                    ),
                ),
            ),
        ),
        (
            'Admin',
            (
                PluginMenuItem(
                    link='plugins:netbox_windows_dhcp:settings',
                    link_text='Settings',
                    permissions=['netbox_windows_dhcp.view_dhcpserver'],
                ),
            ),
        ),
    ),
    icon_class='mdi mdi-server-network',
)
