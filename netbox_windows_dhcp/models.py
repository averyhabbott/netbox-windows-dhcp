from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from netaddr import IPAddress as NetAddrIP, IPNetwork

from netbox.models import NetBoxModel


class DHCPPluginSettings(models.Model):
    """
    Singleton model that stores plugin-wide settings in the database.
    Always accessed via DHCPPluginSettings.load() — never instantiated directly.
    """

    SYNC_MODE_PASSIVE = 'passive'
    SYNC_MODE_ACTIVE = 'active'
    SYNC_MODE_CHOICES = [
        (SYNC_MODE_PASSIVE, 'Passive — sync scope info, do not update IP Addresses'),
        (SYNC_MODE_ACTIVE, 'Active — update NetBox IP Addresses with lease/reservation data'),
    ]

    scope_sync_mode = models.CharField(
        max_length=10,
        choices=SYNC_MODE_CHOICES,
        default=SYNC_MODE_PASSIVE,
        verbose_name='Scope Data Sync Mode',
        help_text=(
            'Active: pull leases/reservations from DHCP servers and update '
            'NetBox IP Address status. Passive: sync scope config only.'
        ),
    )
    push_reservations = models.BooleanField(
        default=False,
        verbose_name='Push Reservations to DHCP Server',
        help_text=(
            'When enabled, NetBox IP Addresses with status "reserved" are '
            'pushed to the DHCP server as reservations.'
        ),
    )
    push_scope_info = models.BooleanField(
        default=False,
        verbose_name='Push Scope Info to DHCP Server',
        help_text=(
            'When enabled, scope configuration (name, range, options) is '
            'pushed from NetBox to the DHCP server on save.'
        ),
    )
    sync_interval = models.PositiveIntegerField(
        default=60,
        verbose_name='Sync Interval (minutes)',
        help_text='How often the background sync job runs (5–1440 minutes).',
    )

    class Meta:
        verbose_name = 'Plugin Settings'

    def __str__(self):
        return 'Windows DHCP Plugin Settings'

    @classmethod
    def load(cls):
        """Return the singleton settings instance, creating it with defaults if absent."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class DHCPServer(NetBoxModel):
    """Represents a Windows DHCP Server reachable via PowerShell Universal API."""

    name = models.CharField(max_length=100, unique=True)
    hostname = models.CharField(max_length=255, help_text='Hostname or IP address of the server')
    port = models.PositiveIntegerField(default=443)
    use_https = models.BooleanField(default=True, verbose_name='Use HTTPS')
    api_key = models.CharField(
        max_length=500,
        blank=True,
        verbose_name='App Token',
        help_text='PSU v5 App Token (Security → App Tokens in the PSU admin console). Sent as Authorization: Bearer. Leave blank if auth is not required.',
    )
    verify_ssl = models.BooleanField(
        default=True,
        verbose_name='Verify SSL Certificate',
        help_text='Uncheck to disable TLS certificate verification (for self-signed certs in test environments).',
    )

    class Meta:
        ordering = ['name']
        verbose_name = 'DHCP Server'
        verbose_name_plural = 'DHCP Servers'

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_windows_dhcp:dhcpserver', args=[self.pk])

    @property
    def base_url(self):
        scheme = 'https' if self.use_https else 'http'
        return f'{scheme}://{self.hostname}:{self.port}/api/dhcp'


class DHCPFailover(NetBoxModel):
    """Represents a Windows DHCP failover relationship between exactly two servers."""

    MODE_LOAD_BALANCE = 'LoadBalance'
    MODE_HOT_STANDBY = 'HotStandby'
    MODE_CHOICES = [
        (MODE_LOAD_BALANCE, 'Load Balance'),
        (MODE_HOT_STANDBY, 'Hot Standby'),
    ]

    name = models.CharField(max_length=100, unique=True)
    primary_server = models.ForeignKey(
        DHCPServer,
        on_delete=models.PROTECT,
        related_name='primary_failovers',
        verbose_name='Primary Server',
    )
    secondary_server = models.ForeignKey(
        DHCPServer,
        on_delete=models.PROTECT,
        related_name='secondary_failovers',
        verbose_name='Secondary Server',
    )
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default=MODE_LOAD_BALANCE)
    max_client_lead_time = models.PositiveIntegerField(
        default=3600,
        verbose_name='Max Client Lead Time (s)',
        help_text='Seconds',
    )
    max_response_delay = models.PositiveIntegerField(
        default=30,
        verbose_name='Max Response Delay (s)',
        help_text='Seconds',
    )
    state_switchover_interval = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name='State Switchover Interval (s)',
        help_text='Seconds. Leave blank to disable automatic switchover.',
    )
    enable_auth = models.BooleanField(
        default=False,
        verbose_name='Enable Authentication',
    )
    shared_secret = models.CharField(
        max_length=500,
        blank=True,
        verbose_name='Shared Secret',
        help_text='Required when authentication is enabled',
    )

    class Meta:
        ordering = ['name']
        verbose_name = 'DHCP Failover'
        verbose_name_plural = 'DHCP Failovers'

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_windows_dhcp:dhcpfailover', args=[self.pk])

    def clean(self):
        super().clean()
        if self.primary_server_id and self.secondary_server_id:
            if self.primary_server_id == self.secondary_server_id:
                raise ValidationError(
                    {'secondary_server': 'Primary and secondary servers must be different.'}
                )
        if self.enable_auth and not self.shared_secret:
            raise ValidationError(
                {'shared_secret': 'A shared secret is required when authentication is enabled.'}
            )


class DHCPOptionCodeDefinition(NetBoxModel):
    """Defines a DHCP option code (e.g. option 3 = Router, option 6 = DNS Servers)."""

    TYPE_STRING = 'String'
    TYPE_IP_ADDRESS = 'IPAddress'
    TYPE_IP_ADDRESS_LIST = 'IPAddressList'
    TYPE_DWORD = 'DWORD'
    TYPE_DWORD_DWORD = 'DWORD DWORD'
    TYPE_BINARY = 'Binary'
    TYPE_ENCAPSULATED = 'Encapsulated'
    TYPE_IPV6_ADDRESS = 'IPv6Address'
    DATA_TYPE_CHOICES = [
        (TYPE_STRING, 'String'),
        (TYPE_IP_ADDRESS, 'IP Address'),
        (TYPE_IP_ADDRESS_LIST, 'IP Address List'),
        (TYPE_DWORD, 'DWORD (32-bit)'),
        (TYPE_DWORD_DWORD, 'DWORD DWORD (64-bit)'),
        (TYPE_BINARY, 'Binary'),
        (TYPE_ENCAPSULATED, 'Encapsulated'),
        (TYPE_IPV6_ADDRESS, 'IPv6 Address'),
    ]

    code = models.PositiveSmallIntegerField(
        unique=True,
        verbose_name='Option Code',
        help_text='DHCP option code number (1–254)',
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    data_type = models.CharField(max_length=20, choices=DATA_TYPE_CHOICES, default=TYPE_STRING)
    is_builtin = models.BooleanField(
        default=False,
        verbose_name='Built-in',
        help_text='Built-in Windows DHCP option — deletion is restricted',
    )
    vendor_class = models.CharField(
        max_length=200,
        blank=True,
        verbose_name='Vendor Class',
        help_text='Leave blank for standard options',
    )

    class Meta:
        ordering = ['code']
        verbose_name = 'DHCP Option Code Definition'
        verbose_name_plural = 'DHCP Option Code Definitions'

    def __str__(self):
        return f'{self.code}: {self.name}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_windows_dhcp:dhcpoptioncodedefinition', args=[self.pk])

    def delete(self, *args, **kwargs):
        if self.is_builtin:
            raise ValidationError('Built-in DHCP option code definitions cannot be deleted.')
        return super().delete(*args, **kwargs)


class DHCPOptionValue(NetBoxModel):
    """
    A reusable DHCP option value. Multiple scopes can reference the same option value.
    Display label: friendly_name if set, otherwise "<code>: <value>".
    """

    option_definition = models.ForeignKey(
        DHCPOptionCodeDefinition,
        on_delete=models.PROTECT,
        related_name='values',
        verbose_name='Option Definition',
    )
    value = models.TextField(help_text='The option value (e.g. IP address, string, hex bytes)')
    friendly_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name='Friendly Name',
        help_text='Optional human-readable label. If blank, displays as "<code>: <value>".',
    )

    class Meta:
        ordering = ['option_definition__code', 'friendly_name', 'value']
        verbose_name = 'DHCP Option Value'
        verbose_name_plural = 'DHCP Option Values'

    def __str__(self):
        if self.friendly_name:
            return self.friendly_name
        code = self.option_definition.code if self.option_definition_id else '?'
        return f'{code}: {self.value}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_windows_dhcp:dhcpoptionvalue', args=[self.pk])


class DHCPScope(NetBoxModel):
    """
    A DHCP scope associated with a NetBox Prefix.
    Many scopes can reference the same Prefix; each scope belongs to exactly one Prefix.
    """

    name = models.CharField(max_length=200)
    prefix = models.ForeignKey(
        'ipam.Prefix',
        on_delete=models.PROTECT,
        related_name='dhcp_scopes',
        verbose_name='Prefix',
    )
    start_ip = models.GenericIPAddressField(verbose_name='Start IP')
    end_ip = models.GenericIPAddressField(verbose_name='End IP')
    router = models.GenericIPAddressField(
        null=True,
        blank=True,
        verbose_name='Router (Option 3)',
        help_text='Default gateway IP address for this scope',
    )
    lease_lifetime = models.PositiveIntegerField(
        default=86400,
        verbose_name='Lease Lifetime',
        help_text='Lease duration in seconds',
    )
    failover = models.ForeignKey(
        DHCPFailover,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='scopes',
        verbose_name='Failover Relationship',
    )
    option_values = models.ManyToManyField(
        DHCPOptionValue,
        blank=True,
        related_name='scopes',
        verbose_name='Option Values',
    )

    class Meta:
        ordering = ['name']
        verbose_name = 'DHCP Scope'
        verbose_name_plural = 'DHCP Scopes'

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_windows_dhcp:dhcpscope', args=[self.pk])

    @property
    def lease_lifetime_display(self) -> str:
        from .utils import lease_lifetime_display
        return lease_lifetime_display(self.lease_lifetime)

    def clean(self):
        super().clean()
        if not self.prefix_id or not self.start_ip or not self.end_ip:
            return

        try:
            prefix_network = IPNetwork(str(self.prefix.prefix))
            start = NetAddrIP(self.start_ip)
            end = NetAddrIP(self.end_ip)
        except Exception:
            return

        if start not in prefix_network:
            raise ValidationError(
                {'start_ip': f'Start IP must be within the prefix {self.prefix.prefix}.'}
            )
        if end not in prefix_network:
            raise ValidationError(
                {'end_ip': f'End IP must be within the prefix {self.prefix.prefix}.'}
            )
        if start > end:
            raise ValidationError(
                {'end_ip': 'End IP must be greater than or equal to the Start IP.'}
            )
