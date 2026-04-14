"""
Signal handlers for netbox-windows-dhcp.

When push_scope_info is enabled, saving a DHCPScope automatically enqueues
a job to push the updated scope to the DHCP server.
"""

import logging

from django.core.exceptions import ValidationError
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


def _patch_ipaddress_clean():
    """
    Monkey-patch IPAddress.clean() to enforce that status 'dhcp' is only allowed
    when the IP falls within a DHCP scope and is not in an exclusion range.

    This runs during form/API validation (full_clean → clean) but NOT during
    direct .save() calls from the background sync — which is intentional, as
    the sync sets statuses based on authoritative data from the DHCP server.
    """
    try:
        from ipam.models import IPAddress
        from netaddr import IPAddress as NetAddrIP, IPNetwork

        _original_clean = IPAddress.clean

        def _dhcp_validated_clean(self):
            _original_clean(self)

            if self.status != 'dhcp':
                return

            # Import here to avoid circular imports at module load time.
            from .models import DHCPScope

            try:
                ip = NetAddrIP(str(self.address.ip))
            except Exception:
                return

            # Find a scope whose prefix contains this IP.
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
                    f'IP addresses with status "dhcp" must fall within a configured DHCP '
                    f'scope prefix. No matching scope found for {self.address.ip}.'
                )

            # Block IPs that fall inside an exclusion range of that scope.
            for ex in matching_scope.exclusion_ranges.all():
                try:
                    if NetAddrIP(ex.start_ip) <= ip <= NetAddrIP(ex.end_ip):
                        raise ValidationError(
                            f'{self.address.ip} falls within exclusion range '
                            f'{ex.start_ip}–{ex.end_ip} of scope "{matching_scope.name}". '
                            f'Excluded IPs cannot have status "dhcp".'
                        )
                except ValidationError:
                    raise
                except Exception:
                    continue

        IPAddress.clean = _dhcp_validated_clean
        logger.debug('Patched IPAddress.clean() with DHCP scope validation')
    except Exception as exc:
        logger.warning(f'Could not patch IPAddress.clean() for DHCP validation: {exc}')


_patch_ipaddress_clean()
