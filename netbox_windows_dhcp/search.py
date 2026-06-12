from netbox.search import SearchIndex

from .models import DHCPLeaseInfo, DHCPScope, DHCPServer


class DHCPLeaseInfoIndex(SearchIndex):
    """
    Indexes DHCPLeaseInfo.lease_hostname for global NetBox search.

    Search results link to the associated IP Address detail page (via
    DHCPLeaseInfo.get_absolute_url). This lets users search for a DHCP
    hostname (e.g. an iDRAC name) and land on the correct IP Address even
    when it differs from dns_name.
    """

    model = DHCPLeaseInfo
    fields = (
        ('lease_hostname', 100),
    )


class DHCPServerIndex(SearchIndex):
    """Indexes DHCP servers by name and hostname for global NetBox search."""

    model = DHCPServer
    fields = (
        ('name', 100),
        ('hostname', 60),
    )
    display_attrs = ('hostname', 'health_status')


class DHCPScopeIndex(SearchIndex):
    """
    Indexes DHCP scopes by name and prefix for global NetBox search.

    'prefix' is a ForeignKey to ipam.Prefix; SearchIndex indexes its string
    form (the CIDR, e.g. '10.0.0.0/24') as text, so scopes are findable by
    typing their prefix.
    """

    model = DHCPScope
    fields = (
        ('name', 100),
        ('prefix', 60),
    )
    display_attrs = ('prefix', 'start_ip', 'end_ip')


indexes = (DHCPLeaseInfoIndex, DHCPServerIndex, DHCPScopeIndex)
