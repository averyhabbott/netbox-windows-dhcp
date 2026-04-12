from netbox.plugins import PluginConfig


class NetBoxWindowsDHCPConfig(PluginConfig):
    name = 'netbox_windows_dhcp'
    verbose_name = 'Windows DHCP'
    description = 'Full integration with Windows DHCP Server via PowerShell Universal'
    version = '1.0.0'
    author = 'Your Organization'
    author_email = 'admin@example.com'
    base_url = 'windows-dhcp'
    min_version = '4.5.0'
    required_settings = []
    default_settings = {}

    def ready(self):
        super().ready()
        self._check_custom_statuses()
        # Register signal handlers
        from . import signals  # noqa: F401

    def _check_custom_statuses(self):
        """Warn if the required custom IP Address statuses are not configured."""
        import logging
        from django.apps import apps
        logger = logging.getLogger('netbox_windows_dhcp')
        try:
            IPAddress = apps.get_model('ipam', 'IPAddress')
            choices = [c[0] for c in IPAddress._meta.get_field('status').choices]
            missing = [s for s in ('dhcp-lease', 'dhcp-reserved') if s not in choices]
            if missing:
                logger.warning(
                    'netbox-windows-dhcp: The following custom IP Address status values are '
                    'not configured: %s. Add them to FIELD_CHOICES in configuration.py. '
                    'See the plugin README for details.',
                    ', '.join(missing),
                )
        except Exception:
            pass


config = NetBoxWindowsDHCPConfig
