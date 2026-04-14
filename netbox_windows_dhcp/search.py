from netbox.search import SearchIndex

from .models import DHCPLeaseInfo


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


indexes = (DHCPLeaseInfoIndex,)
