"""
Data migration: populate DHCPOptionCodeDefinition with all standard Windows DHCP
option codes (is_builtin=True). These match the options Windows DHCP Server knows
about by default.
"""
from django.db import migrations

# (code, name, data_type, description)
WINDOWS_DHCP_BUILTIN_OPTIONS = [
    (1,   'Subnet Mask',                       'IPAddress',     'Client subnet mask'),
    (2,   'Time Offset',                        'DWORD',         'UTC offset in seconds'),
    (3,   'Router',                             'IPAddressList', 'Default gateway(s)'),
    (4,   'Time Server',                        'IPAddressList', 'RFC 868 time servers'),
    (5,   'Name Server',                        'IPAddressList', 'IEN 116 name servers'),
    (6,   'DNS Servers',                        'IPAddressList', 'Domain Name System servers'),
    (7,   'Log Server',                         'IPAddressList', 'MIT-LCS UDP log servers'),
    (8,   'Cookie Server',                      'IPAddressList', 'RFC 865 cookie servers'),
    (9,   'LPR Server',                         'IPAddressList', 'RFC 1179 line printer servers'),
    (10,  'Impress Server',                     'IPAddressList', 'Imagen Impress servers'),
    (11,  'Resource Location Server',           'IPAddressList', 'RFC 887 resource location servers'),
    (12,  'Host Name',                          'String',        'Client hostname'),
    (13,  'Boot File Size',                     'DWORD',         'Size of boot image in 512-byte blocks'),
    (14,  'Merit Dump File',                    'String',        'Path for core dump file'),
    (15,  'DNS Domain Name',                    'String',        'DNS domain name for client'),
    (16,  'Swap Server',                        'IPAddress',     'Client swap server'),
    (17,  'Root Path',                          'String',        'Path to root disk'),
    (18,  'Extensions Path',                    'String',        'TFTP extensions file path'),
    (19,  'IP Layer Forwarding',                'DWORD',         'Enable IP forwarding (0=disable, 1=enable)'),
    (20,  'Non-Local Source Routing',           'DWORD',         'Enable non-local source routing'),
    (21,  'Policy Filter',                      'IPAddressList', 'Filters for non-local datagrams'),
    (22,  'Max DG Reassembly Size',             'DWORD',         'Maximum reassembly buffer size'),
    (23,  'Default IP TTL',                     'DWORD',         'Default TTL for outgoing datagrams'),
    (24,  'Path MTU Aging Timeout',             'DWORD',         'Timeout for Path MTU aging (seconds)'),
    (25,  'Path MTU Plateau Table',             'DWORD DWORD',   'Table of MTU sizes'),
    (26,  'MTU Option',                         'DWORD',         'MTU for this interface'),
    (27,  'All Subnets Local',                  'DWORD',         'Whether all subnets share same MTU'),
    (28,  'Broadcast Address',                  'IPAddress',     'Broadcast address'),
    (29,  'Perform Mask Discovery',             'DWORD',         'Perform subnet mask discovery'),
    (30,  'Mask Supplier',                      'DWORD',         'Respond to mask requests'),
    (31,  'Perform Router Discovery',           'DWORD',         'Perform router discovery'),
    (32,  'Router Solicitation Address',        'IPAddress',     'Router solicitation address'),
    (33,  'Static Route',                       'IPAddressList', 'Static route list (dest/router pairs)'),
    (34,  'Trailer Encapsulation',              'DWORD',         'Negotiate ARP trailers'),
    (35,  'ARP Cache Timeout',                  'DWORD',         'ARP cache timeout (seconds)'),
    (36,  'Ethernet Encapsulation',             'DWORD',         'Use IEEE 802.3 encapsulation'),
    (37,  'TCP Default TTL',                    'DWORD',         'Default TTL for TCP segments'),
    (38,  'TCP Keepalive Interval',             'DWORD',         'TCP keepalive interval (seconds)'),
    (39,  'TCP Keepalive Garbage',              'DWORD',         'Send garbage octet with keepalive'),
    (40,  'Network Info Service Domain',        'String',        'NIS domain name'),
    (41,  'Network Info Servers',               'IPAddressList', 'NIS server addresses'),
    (42,  'NTP Servers',                        'IPAddressList', 'Network Time Protocol servers'),
    (43,  'Vendor-Specific Information',        'Binary',        'Vendor-specific information'),
    (44,  'WINS/NBNS Servers',                  'IPAddressList', 'NetBIOS over TCP/IP name servers'),
    (45,  'NetBIOS over TCP/IP NBDD',           'IPAddressList', 'NetBIOS datagram distribution servers'),
    (46,  'NetBIOS over TCP/IP Node Type',      'DWORD',         '1=B-node, 2=P-node, 4=M-node, 8=H-node'),
    (47,  'NetBIOS over TCP/IP Scope',          'String',        'NetBIOS scope identifier'),
    (48,  'X Window System Font Server',        'IPAddressList', 'X Window font servers'),
    (49,  'X Window System Display Manager',    'IPAddressList', 'X Window display managers'),
    (51,  'IP Address Lease Time',              'DWORD',         'Lease duration in seconds'),
    (54,  'DHCP Server Identifier',             'IPAddress',     'DHCP server IP address'),
    (58,  'Renewal Time',                       'DWORD',         'Time until client enters RENEWING state'),
    (59,  'Rebinding Time',                     'DWORD',         'Time until client enters REBINDING state'),
    (60,  'Vendor Class Identifier',            'String',        'Vendor class identifier string'),
    (64,  'NIS+ Domain',                        'String',        'NIS+ domain name'),
    (65,  'NIS+ Servers',                       'IPAddressList', 'NIS+ server addresses'),
    (66,  'TFTP Server Name',                   'String',        'TFTP server hostname for Option 67'),
    (67,  'Bootfile Name',                      'String',        'Boot file name'),
    (68,  'Mobile IP Home Agent',               'IPAddressList', 'Mobile IP home agents'),
    (69,  'Simple Mail Transport Protocol',     'IPAddressList', 'SMTP servers'),
    (70,  'Post Office Protocol Server',        'IPAddressList', 'POP3 servers'),
    (71,  'Network News Transport Protocol',    'IPAddressList', 'NNTP servers'),
    (72,  'Default World Wide Web Server',      'IPAddressList', 'Default WWW servers'),
    (73,  'Default Finger Server',              'IPAddressList', 'Default Finger servers'),
    (74,  'Default Internet Relay Chat Server', 'IPAddressList', 'Default IRC servers'),
    (75,  'StreetTalk Server',                  'IPAddressList', 'StreetTalk servers'),
    (76,  'StreetTalk Discovery Assistance',    'IPAddressList', 'StreetTalk Directory Assistance servers'),
    (119, 'Domain Search',                      'String',        'DNS domain search list'),
    (121, 'Classless Static Route',             'Binary',        'RFC 3442 classless static routes'),
    (249, 'Microsoft Classless Static Route',   'Binary',        'Microsoft extension: classless static routes'),
    (252, 'Web Proxy Auto-Detect',              'String',        'WPAD proxy auto-configuration URL'),
]


def populate_option_codes(apps, schema_editor):
    DHCPOptionCodeDefinition = apps.get_model('netbox_windows_dhcp', 'DHCPOptionCodeDefinition')
    for code, name, data_type, description in WINDOWS_DHCP_BUILTIN_OPTIONS:
        DHCPOptionCodeDefinition.objects.get_or_create(
            code=code,
            defaults={
                'name': name,
                'data_type': data_type,
                'description': description,
                'is_builtin': True,
            },
        )


def remove_option_codes(apps, schema_editor):
    DHCPOptionCodeDefinition = apps.get_model('netbox_windows_dhcp', 'DHCPOptionCodeDefinition')
    codes = [row[0] for row in WINDOWS_DHCP_BUILTIN_OPTIONS]
    DHCPOptionCodeDefinition.objects.filter(code__in=codes, is_builtin=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_windows_dhcp', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(populate_option_codes, reverse_code=remove_option_codes),
    ]
