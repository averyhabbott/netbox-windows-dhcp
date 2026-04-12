from django.urls import path

from netbox.views.generic import ObjectChangeLogView

from . import views
from .models import (
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)

app_name = 'netbox_windows_dhcp'

urlpatterns = [

    # -----------------------------------------------------------------------
    # DHCPServer
    # -----------------------------------------------------------------------
    path('servers/', views.DHCPServerListView.as_view(), name='dhcpserver_list'),
    path('servers/add/', views.DHCPServerCreateView.as_view(), name='dhcpserver_add'),
    path('servers/delete/', views.DHCPServerBulkDeleteView.as_view(), name='dhcpserver_bulk_delete'),
    path('servers/<int:pk>/', views.DHCPServerView.as_view(), name='dhcpserver'),
    path('servers/<int:pk>/edit/', views.DHCPServerEditView.as_view(), name='dhcpserver_edit'),
    path('servers/<int:pk>/delete/', views.DHCPServerDeleteView.as_view(), name='dhcpserver_delete'),
    path('servers/<int:pk>/changelog/', ObjectChangeLogView.as_view(), {'model': DHCPServer}, name='dhcpserver_changelog'),
    path('servers/<int:pk>/sync/', views.DHCPServerSyncView.as_view(), name='dhcpserver_sync'),
    path('servers/<int:pk>/import/', views.DHCPServerImportView.as_view(), name='dhcpserver_import'),

    # Global sync (all servers)
    path('sync/', views.DHCPGlobalSyncView.as_view(), name='global_sync'),

    # -----------------------------------------------------------------------
    # DHCPFailover
    # -----------------------------------------------------------------------
    path('failover/', views.DHCPFailoverListView.as_view(), name='dhcpfailover_list'),
    path('failover/add/', views.DHCPFailoverCreateView.as_view(), name='dhcpfailover_add'),
    path('failover/delete/', views.DHCPFailoverBulkDeleteView.as_view(), name='dhcpfailover_bulk_delete'),
    path('failover/<int:pk>/', views.DHCPFailoverView.as_view(), name='dhcpfailover'),
    path('failover/<int:pk>/edit/', views.DHCPFailoverEditView.as_view(), name='dhcpfailover_edit'),
    path('failover/<int:pk>/delete/', views.DHCPFailoverDeleteView.as_view(), name='dhcpfailover_delete'),
    path('failover/<int:pk>/changelog/', ObjectChangeLogView.as_view(), {'model': DHCPFailover}, name='dhcpfailover_changelog'),

    # -----------------------------------------------------------------------
    # DHCPOptionCodeDefinition
    # -----------------------------------------------------------------------
    path('option-codes/', views.DHCPOptionCodeDefinitionListView.as_view(), name='dhcpoptioncodedefinition_list'),
    path('option-codes/add/', views.DHCPOptionCodeDefinitionCreateView.as_view(), name='dhcpoptioncodedefinition_add'),
    path('option-codes/delete/', views.DHCPOptionCodeDefinitionBulkDeleteView.as_view(), name='dhcpoptioncodedefinition_bulk_delete'),
    path('option-codes/<int:pk>/', views.DHCPOptionCodeDefinitionView.as_view(), name='dhcpoptioncodedefinition'),
    path('option-codes/<int:pk>/edit/', views.DHCPOptionCodeDefinitionEditView.as_view(), name='dhcpoptioncodedefinition_edit'),
    path('option-codes/<int:pk>/delete/', views.DHCPOptionCodeDefinitionDeleteView.as_view(), name='dhcpoptioncodedefinition_delete'),
    path('option-codes/<int:pk>/changelog/', ObjectChangeLogView.as_view(), {'model': DHCPOptionCodeDefinition}, name='dhcpoptioncodedefinition_changelog'),

    # -----------------------------------------------------------------------
    # DHCPOptionValue
    # -----------------------------------------------------------------------
    path('option-values/', views.DHCPOptionValueListView.as_view(), name='dhcpoptionvalue_list'),
    path('option-values/add/', views.DHCPOptionValueCreateView.as_view(), name='dhcpoptionvalue_add'),
    path('option-values/delete/', views.DHCPOptionValueBulkDeleteView.as_view(), name='dhcpoptionvalue_bulk_delete'),
    path('option-values/<int:pk>/', views.DHCPOptionValueView.as_view(), name='dhcpoptionvalue'),
    path('option-values/<int:pk>/edit/', views.DHCPOptionValueEditView.as_view(), name='dhcpoptionvalue_edit'),
    path('option-values/<int:pk>/delete/', views.DHCPOptionValueDeleteView.as_view(), name='dhcpoptionvalue_delete'),
    path('option-values/<int:pk>/changelog/', ObjectChangeLogView.as_view(), {'model': DHCPOptionValue}, name='dhcpoptionvalue_changelog'),

    # -----------------------------------------------------------------------
    # DHCPScope
    # -----------------------------------------------------------------------
    path('scopes/', views.DHCPScopeListView.as_view(), name='dhcpscope_list'),
    path('scopes/add/', views.DHCPScopeCreateView.as_view(), name='dhcpscope_add'),
    path('scopes/edit/', views.DHCPScopeBulkEditView.as_view(), name='dhcpscope_bulk_edit'),
    path('scopes/delete/', views.DHCPScopeBulkDeleteView.as_view(), name='dhcpscope_bulk_delete'),
    path('scopes/<int:pk>/', views.DHCPScopeView.as_view(), name='dhcpscope'),
    path('scopes/<int:pk>/edit/', views.DHCPScopeEditView.as_view(), name='dhcpscope_edit'),
    path('scopes/<int:pk>/delete/', views.DHCPScopeDeleteView.as_view(), name='dhcpscope_delete'),
    path('scopes/<int:pk>/changelog/', ObjectChangeLogView.as_view(), {'model': DHCPScope}, name='dhcpscope_changelog'),

    # -----------------------------------------------------------------------
    # Settings
    # -----------------------------------------------------------------------
    path('settings/', views.SettingsView.as_view(), name='settings'),
]
