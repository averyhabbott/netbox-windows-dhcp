from django.urls import include, path

from utilities.urls import get_model_urls

from . import views

app_name = 'netbox_windows_dhcp'

urlpatterns = [

    # DHCPServer
    path('servers/',           include(get_model_urls('netbox_windows_dhcp', 'dhcpserver', detail=False))),
    path('servers/<int:pk>/',  include(get_model_urls('netbox_windows_dhcp', 'dhcpserver'))),

    # DHCPFailover
    path('failover/',          include(get_model_urls('netbox_windows_dhcp', 'dhcpfailover', detail=False))),
    path('failover/<int:pk>/', include(get_model_urls('netbox_windows_dhcp', 'dhcpfailover'))),

    # DHCPOptionCodeDefinition
    path('option-codes/',          include(get_model_urls('netbox_windows_dhcp', 'dhcpoptioncodedefinition', detail=False))),
    path('option-codes/<int:pk>/', include(get_model_urls('netbox_windows_dhcp', 'dhcpoptioncodedefinition'))),

    # DHCPOptionValue
    path('option-values/',          include(get_model_urls('netbox_windows_dhcp', 'dhcpoptionvalue', detail=False))),
    path('option-values/<int:pk>/', include(get_model_urls('netbox_windows_dhcp', 'dhcpoptionvalue'))),

    # DHCPScope
    path('scopes/',          include(get_model_urls('netbox_windows_dhcp', 'dhcpscope', detail=False))),
    path('scopes/<int:pk>/', include(get_model_urls('netbox_windows_dhcp', 'dhcpscope'))),

    # DHCPExclusionRange (no list view; only add + detail)
    path('exclusion-ranges/',          include(get_model_urls('netbox_windows_dhcp', 'dhcpexclusionrange', detail=False))),
    path('exclusion-ranges/<int:pk>/', include(get_model_urls('netbox_windows_dhcp', 'dhcpexclusionrange'))),

    # Cross-model / plugin-wide pages (not bound to a single model PK)
    path('sync/',                        views.DHCPGlobalSyncView.as_view(),                    name='global_sync'),
    path('servers/cert/fetch/',          views.DHCPServerCertFetchView.as_view(),               name='dhcpserver_cert_fetch'),
    path('servers/new/test-connection/', views.DHCPServerTestConnectionView.as_view(),          name='dhcpserver_test_connection_new'),
    path('maintenance/',                 views.DHCPCurrentMaintenanceView.as_view(),            name='current_maintenance'),
    path('maintenance/disable/',         views.DHCPCurrentMaintenanceBulkDisableView.as_view(), name='current_maintenance_bulk_disable'),
    path('settings/',                    views.SettingsView.as_view(),                          name='settings'),
    path('settings/sync/',               views.ScheduleSyncView.as_view(),                      name='schedule_sync'),
]
