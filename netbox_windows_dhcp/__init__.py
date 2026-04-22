import logging

from netbox.plugins import PluginConfig

logger = logging.getLogger('netbox_windows_dhcp')


def _ensure_custom_fields(sender, **kwargs):
    """
    Create the dhcp_client_id custom field on IPAddress if it doesn't exist.
    Called via post_migrate so the database is guaranteed to be ready.
    """
    try:
        from django.contrib.contenttypes.models import ContentType
        from extras.models import CustomField
        from ipam.models import IPAddress

        # NetBox 4.x uses ObjectType (a ContentType proxy) as the M2M target for
        # CustomField.object_types.  Fall back to raw ContentType if unavailable.
        try:
            from core.models import ObjectType
            ip_obj_type = ObjectType.objects.get_for_model(IPAddress)
        except (ImportError, AttributeError):
            ip_obj_type = ContentType.objects.get_for_model(IPAddress)

        cf, created = CustomField.objects.get_or_create(
            name='dhcp_client_id',
            defaults={
                'label': 'DHCP Client ID',
                'type': 'text',
                'description': 'DHCP client MAC address — populated automatically by Windows DHCP sync',
                'required': False,
            },
        )
        if not cf.object_types.filter(pk=ip_obj_type.pk).exists():
            cf.object_types.add(ip_obj_type)
        if created:
            logger.info('Registered custom field dhcp_client_id on IPAddress')
    except Exception as exc:
        logger.warning(f'Could not register dhcp_client_id custom field: {exc}')


class NetBoxWindowsDHCPConfig(PluginConfig):
    name = 'netbox_windows_dhcp'
    verbose_name = 'Windows DHCP'
    description = 'Full integration with Windows DHCP Server via PowerShell Universal'
    version = '1.0.0'
    author = 'Avery Abbott'
    author_email = 'averyhabbott@yahoo.com'
    base_url = 'windows-dhcp'
    min_version = '4.5.0'
    required_settings = []
    default_settings = {}

    def ready(self):
        super().ready()
        from django.db.models.signals import post_migrate
        post_migrate.connect(_ensure_custom_fields, sender=self)
        from . import signals  # noqa: F401
        from . import background_tasks  # noqa: F401 — registers DHCPSyncJob with system_job scheduler


config = NetBoxWindowsDHCPConfig
