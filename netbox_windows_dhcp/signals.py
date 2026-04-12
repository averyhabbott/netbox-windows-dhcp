"""
Signal handlers for netbox-windows-dhcp.

When push_scope_info is enabled, saving a DHCPScope automatically enqueues
a job to push the updated scope to the DHCP server.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger('netbox_windows_dhcp')


@receiver(post_save, sender='netbox_windows_dhcp.DHCPScope')
def dhcpscope_post_save(sender, instance, created, **kwargs):
    """When a DHCPScope is saved and push_scope_info is on, push to DHCP server."""
    from .models import DHCPPluginSettings
    if not DHCPPluginSettings.load().push_scope_info:
        return
    try:
        from .background_tasks import DHCPServerSyncJob
        from .models import DHCPServer
        # Push to all servers that have scopes in this prefix's failover or any server
        servers = DHCPServer.objects.all()
        for server in servers:
            DHCPServerSyncJob.enqueue(server)
    except Exception as exc:
        logger.warning(f'Failed to enqueue scope push after save of {instance}: {exc}')
