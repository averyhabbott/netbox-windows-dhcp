"""
Signal handlers for netbox-windows-dhcp.

When push_scope_info is enabled, saving a DHCPScope automatically enqueues
a job to push the updated scope to the DHCP server.
"""

import logging

from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
from django.dispatch import receiver
from netbox.signals import post_clean

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


@receiver(post_clean)
def validate_dhcp_ip_status(sender, instance, **kwargs):
    """
    Validate that IPs with status 'dhcp' fall within a configured DHCP scope
    and are not inside an exclusion range.

    Runs during form and API validation (full_clean → clean → post_clean signal)
    but NOT during direct .save() calls from the background sync — intentional,
    as the sync writes authoritative data from the DHCP server.
    """
    from ipam.models import IPAddress
    if not isinstance(instance, IPAddress):
        return
    from .models import DHCPPluginSettings, DHCPScope
    lease_status = DHCPPluginSettings.load().lease_status
    if instance.status != lease_status:
        return

    from netaddr import IPAddress as NetAddrIP, IPNetwork

    try:
        ip = NetAddrIP(str(instance.address.ip))
    except Exception:
        return

    matching_scope = None
    for scope in DHCPScope.objects.select_related('prefix').prefetch_related('exclusion_ranges'):
        try:
            if ip in IPNetwork(str(scope.prefix.prefix)):
                matching_scope = scope
                break
        except Exception:
            continue

    if matching_scope is None:
        raise ValidationError(
            f'IP addresses with status "{lease_status}" must fall within a configured DHCP '
            f'scope prefix. No matching scope found for {instance.address.ip}.'
        )

    for ex in matching_scope.exclusion_ranges.all():
        try:
            if NetAddrIP(ex.start_ip) <= ip <= NetAddrIP(ex.end_ip):
                raise ValidationError(
                    f'{instance.address.ip} falls within exclusion range '
                    f'{ex.start_ip}–{ex.end_ip} of scope "{matching_scope.name}". '
                    f'Excluded IPs cannot have status "{lease_status}".'
                )
        except ValidationError:
            raise
        except Exception:
            continue
