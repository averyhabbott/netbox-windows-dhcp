"""
One-time fix after upgrading: move sync-created IPs that have no VRF into the VRF of
the DHCP scope prefix they belong to.

Older versions of the sync matched IPs by address only and created them with no VRF.
The sync now only looks at IPs in the scope prefix's VRF, so without this fix it would
stop seeing those IPs and create duplicates inside the VRF. Run it once, before the
first sync after upgrading:

    python manage.py dhcp_fix_ip_vrf --dry-run
    python manage.py dhcp_fix_ip_vrf

Only touches IPs the sync manages: no VRF, status is the configured lease/reservation
status, and the IP has a DHCPLeaseInfo row. The IP is updated in place, so its history,
tags and assignments carry over. Skipped (and listed): IPs whose address already exists
in the target VRF, and IPs inside scope prefixes in more than one VRF. IPs inside a
no-VRF scope prefix are already correct and left alone. Sync-protected IPs (tag or
protected prefix) are skipped. Changes are change-logged as DHCP-Sync-Service.
"""

import ipaddress

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Move sync-created no-VRF IPs into their DHCP scope prefix's VRF."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without saving anything.',
        )

    def handle(self, *args, dry_run=False, **options):
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

        scope_networks = []  # (network, vrf_id, vrf, scope_name)
        for scope in DHCPScope.objects.select_related('prefix__vrf').order_by('name'):
            prefix = scope.prefix
            if prefix is None:
                continue
            scope_networks.append((
                ipaddress.ip_network(str(prefix.prefix), strict=False),
                prefix.vrf_id, prefix.vrf, scope.name,
            ))
        vrf_networks = [n for n in scope_networks if n[1] is not None]
        global_networks = [n[0] for n in scope_networks if n[1] is None]

        # Candidate IPs: no VRF, sync-managed, inside at least one VRF'd scope prefix.
        candidates = {}
        for network, _vrf_id, _vrf, _name in vrf_networks:
            for ip_obj in (
                IPAddress.objects
                .filter(
                    address__net_contained_or_equal=str(network),
                    vrf__isnull=True,
                    status__in=statuses,
                    dhcp_lease_info__isnull=False,
                )
                .prefetch_related('tags')
            ):
                candidates[ip_obj.pk] = ip_obj

        n_moved = 0
        n_skipped_protected = 0
        n_skipped_exists = 0
        n_skipped_ambiguous = 0
        with _change_logging():
            for ip_obj in sorted(candidates.values(), key=lambda i: i.address):
                addr = ipaddress.ip_address(str(ip_obj.address.ip))  # netaddr → stdlib
                if any(addr in net for net in global_networks):
                    continue  # also inside a no-VRF scope prefix — already where it belongs
                if (protect_tag and protect_tag in ip_obj.tags.slugs()) or any(
                    addr in net for net in protected_networks
                ):
                    n_skipped_protected += 1
                    continue

                targets = {}
                for network, vrf_id, vrf, scope_name in vrf_networks:
                    if addr in network:
                        targets.setdefault(vrf_id, (vrf, scope_name))
                if len(targets) > 1:
                    names = ', '.join(sorted(v.name for v, _ in targets.values()))
                    self.stdout.write(
                        f'Skipped {ip_obj.address}: inside scope prefixes in more than one VRF ({names})'
                    )
                    n_skipped_ambiguous += 1
                    continue
                (vrf_id, (vrf, scope_name)), = targets.items()

                if IPAddress.objects.filter(vrf_id=vrf_id, address__net_host=str(addr)).exists():
                    self.stdout.write(
                        f'Skipped {ip_obj.address}: address already exists in VRF {vrf.name}'
                    )
                    n_skipped_exists += 1
                    continue

                self.stdout.write(
                    f'{"Would move" if dry_run else "Moved"} {ip_obj.address} '
                    f'(no VRF) → VRF {vrf.name} (scope {scope_name})'
                )
                if not dry_run:
                    ip_obj.snapshot()
                    ip_obj.vrf_id = vrf_id
                    ip_obj.save(update_fields=['vrf', 'last_updated'])
                n_moved += 1

        verb = 'would be moved' if dry_run else 'moved'
        self.stdout.write(self.style.SUCCESS(
            f'{n_moved} IP(s) {verb}; skipped {n_skipped_exists} already in the VRF, '
            f'{n_skipped_ambiguous} in more than one VRF, {n_skipped_protected} protected.'
        ))
