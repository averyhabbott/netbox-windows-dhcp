from netbox.api.routers import NetBoxRouter
from . import views

app_name = 'netbox_windows_dhcp-api'

router = NetBoxRouter()
router.register('servers', views.DHCPServerViewSet)
router.register('failover', views.DHCPFailoverViewSet)
router.register('option-codes', views.DHCPOptionCodeDefinitionViewSet)
router.register('option-values', views.DHCPOptionValueViewSet)
router.register('scopes', views.DHCPScopeViewSet)

urlpatterns = router.urls
