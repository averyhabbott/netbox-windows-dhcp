"""
On-demand backfill: set the tenant on sync-managed IP addresses to match the tenant
of their DHCP scope's prefix.

The sync only sets tenant when it creates an IP; it never changes the tenant of an
existing IP. Run this to bring IPs created before that behaviour (or re-tenanted
prefixes) in line.

    python manage.py dhcp_apply_prefix_tenant --dry-run
    python manage.py dhcp_apply_prefix_tenant
    python manage.py dhcp_apply_prefix_tenant --overwrite

Only touches IPs the sync manages: status is the configured lease/reservation status
and the IP has a DHCPLeaseInfo row. Sync-protected IPs (tag or protected prefix) and
scopes whose prefix has no tenant are skipped. Changes are change-logged as
DHCP-Sync-Service.
"""

import ipaddress

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Set the tenant of sync-managed IPs to their DHCP scope prefix's tenant."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without saving anything.',
        )
        parser.add_argument(
            '--overwrite', action='store_true',
            help='Also replace a tenant that differs from the prefix (default: only fill blanks).',
        )

    def handle(self, *args, dry_run=False, overwrite=False, **options):
        from ipam.models import IPAddress, Prefix

        from ...background_tasks import _change_logging, _load_settings
        from ...models import DHCPScope

        cfg = _load_settings()
        statuses = (cfg.lease_status, cfg.reservation_status)
        protect_tag = cfg.sync_protect_tag.slug if cfg.sync_protect_tag_id else ''
        protected_networks = set()
        if protect_tag:
            for p in Prefix.objects.filter(tags__slug=protect_tag):
                protected_networks.add(ipaddress.ip_network(str(p.prefix), strict=False))

        n_changed = 0
        n_skipped_protected = 0
        with _change_logging():
            for scope in DHCPScope.objects.select_related('prefix__tenant').order_by('name'):
                prefix = scope.prefix
                if prefix is None or prefix.tenant_id is None:
                    continue
                ips = (
                    IPAddress.objects
                    .filter(
                        address__net_contained_or_equal=str(prefix.prefix),
                        vrf_id=prefix.vrf_id,
                        status__in=statuses,
                        dhcp_lease_info__isnull=False,
                    )
                    .exclude(tenant_id=prefix.tenant_id)
                    .select_related('tenant')
                    .prefetch_related('tags')
                )
                if not overwrite:
                    ips = ips.filter(tenant__isnull=True)

                for ip_obj in ips:
                    addr = ipaddress.ip_address(str(ip_obj.address.ip))  # netaddr → stdlib
                    if (protect_tag and protect_tag in ip_obj.tags.slugs()) or any(
                        addr in net for net in protected_networks
                    ):
                        n_skipped_protected += 1
                        continue
                    old = ip_obj.tenant.name if ip_obj.tenant else '(none)'
                    self.stdout.write(
                        f'{"Would set" if dry_run else "Set"} {ip_obj.address} tenant '
                        f'{old} → {prefix.tenant.name} (scope {scope.name})'
                    )
                    if not dry_run:
                        ip_obj.snapshot()
                        ip_obj.tenant_id = prefix.tenant_id
                        ip_obj.save(update_fields=['tenant', 'last_updated'])
                    n_changed += 1

        verb = 'would be updated' if dry_run else 'updated'
        self.stdout.write(self.style.SUCCESS(
            f'{n_changed} IP(s) {verb}; {n_skipped_protected} protected IP(s) skipped.'
        ))
