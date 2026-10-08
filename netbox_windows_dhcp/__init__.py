import logging

from netbox.plugins import PluginConfig

__version__ = '2.0.1'

logger = logging.getLogger('netbox_windows_dhcp')


def _create_service_user(sender, **kwargs):
    """Create the DHCP-Sync-Service account if it doesn't exist. Idempotent."""
    try:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        User.objects.get_or_create(
            username='DHCP-Sync-Service',
            defaults={'is_active': False},
        )
    except Exception as exc:
        logger.warning(f'Could not create DHCP-Sync-Service user: {exc}')


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


def _ensure_invalid_hostname_tag(sender, **kwargs):
    """Create the invalid-client-hostname tag if it doesn't exist. Idempotent."""
    try:
        from .background_tasks import _invalid_hostname_tag
        _invalid_hostname_tag()
    except Exception as exc:
        logger.warning(f'Could not create invalid-client-hostname tag: {exc}')


def _ensure_unassigned_scopes_filter(sender, **kwargs):
    """
    Create the shared "Unassigned Scopes" saved filter (scopes with no prefix) if no filter
    with that name or slug exists. Idempotent; an existing filter is never changed.
    """
    try:
        from django.db.models import Q
        from core.models import ObjectType
        from extras.models import SavedFilter
        from .models import DHCPScope

        if SavedFilter.objects.filter(Q(name='Unassigned Scopes') | Q(slug='unassigned-scopes')).exists():
            return
        saved_filter = SavedFilter.objects.create(
            name='Unassigned Scopes',
            slug='unassigned-scopes',
            description='DHCP scopes with no prefix',
            shared=True,
            parameters={'has_prefix': ['false']},
        )
        saved_filter.object_types.add(ObjectType.objects.get_for_model(DHCPScope))
        logger.info('Created the Unassigned Scopes saved filter')
    except Exception as exc:
        logger.warning(f'Could not create the Unassigned Scopes saved filter: {exc}')


class NetBoxWindowsDHCPConfig(PluginConfig):
    name = 'netbox_windows_dhcp'
    verbose_name = 'Windows DHCP'
    description = 'Full integration with Windows DHCP Server via PowerShell Universal'
    version = __version__
    author = 'Avery Abbott'
    author_email = 'averyhabbott@yahoo.com'
    base_url = 'windows-dhcp'
    min_version = '4.5.0'
    max_version = '4.6.99'
    required_settings = []
    default_settings = {}

    def ready(self):
        super().ready()
        from django.db.models.signals import post_migrate
        post_migrate.connect(_create_service_user, sender=self)
        post_migrate.connect(_ensure_custom_fields, sender=self)
        post_migrate.connect(_ensure_invalid_hostname_tag, sender=self)
        post_migrate.connect(_ensure_unassigned_scopes_filter, sender=self)
        from . import signals  # noqa: F401
        from . import background_tasks  # noqa: F401
        from .tables import register_core_table_columns
        register_core_table_columns()


config = NetBoxWindowsDHCPConfig
