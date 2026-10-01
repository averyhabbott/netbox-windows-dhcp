from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.deletion import ProtectedError
from django.urls import reverse
from netaddr import IPAddress as NetAddrIP, IPNetwork

from netbox.models import NetBoxModel
from netbox.models.features import ChangeLoggingMixin
from utilities.querysets import RestrictedQuerySet

from .choices import (
    DHCPFailoverModeChoices,
    DHCPOptionDataTypeChoices,
    DHCPServerAccessChoices,
    DHCPServerHealthChoices,
    SyncLogLevelChoices,
    SyncQueueChoices,
)


class DHCPPluginSettings(ChangeLoggingMixin, models.Model):
    """
    Singleton model that stores plugin-wide settings in the database.
    Always accessed via DHCPPluginSettings.load() — never instantiated directly.
    Saves from the Settings page are recorded in NetBox's changelog.
    """

    sync_ip_addresses = models.BooleanField(
        default=False,
        verbose_name='Sync IP Addresses from Leases & Reservations',
        help_text=(
            'When enabled, pull leases and reservations from DHCP servers and '
            'create/update/delete NetBox IP Address records with status, DNS name, '
            'and client MAC.'
        ),
    )
    lease_status = models.CharField(
        max_length=50,
        default='dhcp',
        verbose_name='DHCP Lease Status',
        help_text=(
            'IP Address status assigned to active DHCP leases by the sync. '
            'Changing this mid-deployment will cause the next sync to update all managed IPs to the new status.'
        ),
    )
    reservation_status = models.CharField(
        max_length=50,
        default='reserved',
        verbose_name='DHCP Reservation Status',
        help_text=(
            'IP Address status assigned to DHCP reservations by the sync, and the status that '
            'triggers a push to the DHCP server when Push Reservations is enabled.'
        ),
    )
    push_reservations = models.BooleanField(
        default=False,
        verbose_name='Push Reservations to DHCP Server',
        help_text=(
            'When enabled, NetBox is the source of truth for reservations: IP Addresses with the '
            'configured reservation status are created, updated and deleted on the DHCP server, '
            'and server reservations NetBox doesn\'t have are deleted. When disabled, the DHCP '
            'server is the source of truth and NetBox mirrors its reservations. A server with a '
            'read-only API key always syncs as if this were disabled.'
        ),
    )
    push_scope_info = models.BooleanField(
        default=False,
        verbose_name='Push Scope Info to DHCP Server',
        help_text=(
            'When enabled, NetBox is the source of truth for scopes: name, description, range, '
            'router, lease time, active state, failover, options and exclusions are pushed to the '
            'DHCP server, NetBox-only scopes are created on it, and scopes on the server but not '
            'in NetBox are removed from it. When disabled, the DHCP server is the source of truth: '
            'scope settings are pulled on every sync, new server scopes are imported, and scopes '
            'removed from the server are deleted from NetBox. Maintenance mode and the server and '
            'failover sync settings apply either way. A server with a read-only API key always '
            'syncs as if this were disabled.'
        ),
    )
    reservation_placeholders = models.BooleanField(
        default=False,
        verbose_name='Placeholder Reservations',
        help_text=(
            'Reserving an IP inside a DHCP scope without a client ID does nothing on its own, '
            'because the DHCP server can still hand that IP out. When enabled (with Push '
            'Reservations on), such IPs get a placeholder client ID (ba-dc-0d-ed-…) in NetBox '
            'and on the DHCP server so the server stops handing them out. Turning this off '
            'stops new placeholders; existing ones are left as they are.'
        ),
    )
    sync_interval = models.PositiveIntegerField(
        default=60,
        verbose_name='Sync Interval (minutes)',
        help_text='How often the background sync job runs (5–1440 minutes).',
    )
    QUEUE_HIGH = SyncQueueChoices.HIGH
    QUEUE_DEFAULT = SyncQueueChoices.DEFAULT
    QUEUE_LOW = SyncQueueChoices.LOW

    sync_queue = models.CharField(
        max_length=20,
        choices=SyncQueueChoices,
        default=SyncQueueChoices.DEFAULT,
        verbose_name='Sync Job Queue',
        help_text="Worker queue priority used for the plugin's sync, import, push and PSU script update jobs.",
    )
    sync_job_timeout = models.PositiveIntegerField(
        default=300,
        verbose_name='Sync Job Timeout (seconds)',
        help_text=(
            'Maximum wall-clock seconds a sync job may run before RQ kills it. '
            'Default 300 matches RQ_DEFAULT_TIMEOUT. Increase for servers with '
            'large scope counts. CONN_MAX_AGE is automatically aligned to this '
            'value at job start.'
        ),
    )
    sync_protect_tag = models.ForeignKey(
        'extras.Tag',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Sync-Protected Tag',
        help_text=(
            'IP Addresses carrying this tag, or inside a prefix carrying it, are protected from '
            'being overwritten by a sync: status, DNS name, and the IP itself are never modified '
            'or removed by the sync. The tag does not stop pushes to the DHCP server. '
            'Leave blank to disable.'
        ),
    )
    sync_protect_update_client_id = models.BooleanField(
        default=False,
        verbose_name='Update Client ID for Protected IPs',
        help_text=(
            'When enabled, the sync updates the DHCP Client ID field on protected IPs to match '
            'the DHCP server\'s active lease (useful after a server replacement when the client '
            'MAC changes). All other sync writes are still blocked for protected IPs.'
        ),
    )
    create_missing_prefixes = models.BooleanField(
        default=True,
        verbose_name='Create Missing Prefixes on Import',
        help_text=(
            'When enabled, a scope learned from a DHCP server (by Import or the sync) whose prefix '
            'isn\'t in NetBox gets one, created in the Default Scope VRF. When disabled, the scope '
            'is created without a prefix instead (see the Unassigned Scopes saved filter). '
            'Disable if Prefixes are managed by another source.'
        ),
    )
    api_enabled = models.BooleanField(
        default=True,
        verbose_name='API Enabled',
        help_text='When disabled, all plugin REST API endpoints return 503 Service Unavailable.',
    )
    sync_log_level = models.CharField(
        max_length=10,
        choices=SyncLogLevelChoices,
        default=SyncLogLevelChoices.DEBUG,
        verbose_name='Sync Job Log Level',
        help_text=(
            'Minimum severity written to the job log for DHCP sync/push/delete jobs. '
            'Lower levels produce more detail but slow down large syncs. '
            'Does not affect the Import or Update PSU Scripts jobs.'
        ),
    )

    class Meta:
        verbose_name = 'Plugin Settings'

    def __str__(self):
        return 'Windows DHCP Plugin Settings'

    def get_absolute_url(self):
        return reverse('plugins:netbox_windows_dhcp:settings')

    @classmethod
    def load(cls):
        """Return the singleton settings instance, creating it with defaults if absent.

        PLUGINS_CONFIG boolean overrides are applied in memory after loading so all
        callers automatically receive the effective values without any call-site changes.
        """
        obj, _ = cls.objects.get_or_create(pk=1)
        try:
            from django.conf import settings as django_settings
            plugin_cfg = getattr(django_settings, 'PLUGINS_CONFIG', {}).get('netbox_windows_dhcp', {})
            for cfg_key, field_name in {
                'sync_ips_from_dhcp': 'sync_ip_addresses',
                'push_reservations': 'push_reservations',
                'push_scope_info': 'push_scope_info',
            }.items():
                val = plugin_cfg.get(cfg_key)
                if val is not None:
                    setattr(obj, field_name, bool(val))
        except Exception:
            pass
        return obj


class DHCPServer(NetBoxModel):
    """Represents a Windows DHCP Server reachable via PowerShell Universal API."""

    name = models.CharField(max_length=100, unique=True)
    hostname = models.CharField(max_length=255, help_text='Hostname or IP address of the server')
    port = models.PositiveIntegerField(default=443)
    use_https = models.BooleanField(default=True, verbose_name='Use HTTPS')
    api_key = models.CharField(
        max_length=2000,
        blank=True,
        verbose_name='App Token',
        help_text='PSU v5 App Token (Security → App Tokens in the PSU admin console). Sent as Authorization: Bearer.',
    )
    verify_ssl = models.BooleanField(
        default=True,
        verbose_name='Verify SSL Certificate',
        help_text=(
            'Uncheck to disable TLS certificate verification. '
            'For self-signed certs, use "Import HTTPS Certificate" instead of disabling verification.'
        ),
    )
    ca_cert = models.TextField(
        blank=True,
        default='',
        verbose_name='Stored CA Certificate',
        help_text=(
            'PEM-encoded CA certificate imported via "Import HTTPS Certificate". '
            'Used for TLS verification when Verify SSL is enabled.'
        ),
    )
    ca_cert_expiry = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='CA Certificate Expiry',
    )
    sync_standalone_scopes = models.BooleanField(
        default=True,
        verbose_name='Sync Standalone Scopes',
        help_text=(
            'When enabled, scopes with no failover relationship are included in sync operations for this server.'
        ),
    )
    default_scope_vrf = models.ForeignKey(
        'ipam.VRF',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Default Scope VRF',
        help_text=(
            'Where standalone scopes learned from this server look for (or create) their prefix. '
            'Blank means the global VRF. Changing it only affects scopes learned from then on.'
        ),
    )

    # Maintenance mode
    maintenance_mode = models.BooleanField(default=False, verbose_name='Maintenance Mode')
    maintenance_enabled_at = models.DateTimeField(null=True, blank=True)
    maintenance_enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    maintenance_notes = models.TextField(blank=True, default='')

    # Health tracking
    HEALTH_UNKNOWN = DHCPServerHealthChoices.UNKNOWN
    HEALTH_HEALTHY = DHCPServerHealthChoices.HEALTHY
    HEALTH_UNREACHABLE = DHCPServerHealthChoices.UNREACHABLE

    health_status = models.CharField(
        max_length=20,
        choices=DHCPServerHealthChoices,
        default=DHCPServerHealthChoices.UNKNOWN,
        verbose_name='Health Status',
    )
    last_health_check = models.DateTimeField(null=True, blank=True)
    health_error = models.TextField(blank=True, default='')

    # API key write access, detected via ping_write() alongside the health check
    ACCESS_UNKNOWN = DHCPServerAccessChoices.UNKNOWN
    ACCESS_RO = DHCPServerAccessChoices.RO
    ACCESS_RW = DHCPServerAccessChoices.RW

    access_level = models.CharField(
        max_length=20,
        choices=DHCPServerAccessChoices,
        default=DHCPServerAccessChoices.UNKNOWN,
        verbose_name='Access Level',
    )
    last_sync_at = models.DateTimeField(null=True, blank=True, verbose_name='Last Sync')
    last_sync_error = models.TextField(blank=True, default='')
    psu_script_version = models.CharField(
        max_length=50, blank=True, default='', verbose_name='Remote Scripts Version'
    )

    class Meta:
        ordering = ['name']
        verbose_name = 'DHCP Server'
        verbose_name_plural = 'DHCP Servers'

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_windows_dhcp:dhcpserver', args=[self.pk])

    def serialize_object(self, exclude=None):
        data = super().serialize_object(exclude=[*(exclude or []), 'last_sync_at', 'last_health_check'])
        # The changelog and webhook snapshots must never carry the App Token itself
        if 'api_key' in data:
            data['api_key'] = '********' if self.api_key else ''
        return data

    @property
    def base_url(self):
        scheme = 'https' if self.use_https else 'http'
        return f'{scheme}://{self.hostname}:{self.port}/api/dhcp'


class DHCPFailover(NetBoxModel):
    """Represents a Windows DHCP failover relationship between exactly two servers."""

    MODE_LOAD_BALANCE = DHCPFailoverModeChoices.LOAD_BALANCE
    MODE_HOT_STANDBY = DHCPFailoverModeChoices.HOT_STANDBY
    MODE_CHOICES = DHCPFailoverModeChoices

    # Not unique: Windows keeps names unique per server, but two separate server pairs
    # (dev and prod, say) can use the same name. Look failovers up with find_failover().
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=200, blank=True)
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
    mode = models.CharField(
        max_length=20,
        choices=DHCPFailoverModeChoices,
        default=DHCPFailoverModeChoices.LOAD_BALANCE,
    )
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
    sync_enabled = models.BooleanField(
        default=True,
        verbose_name='Sync Enabled',
        help_text=(
            'When enabled, scopes using this failover relationship are included in sync operations.'
        ),
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
    default_scope_vrf = models.ForeignKey(
        'ipam.VRF',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Default Scope VRF',
        help_text=(
            'Where scopes learned for this failover look for (or create) their prefix. '
            'Blank means the global VRF. Changing it only affects scopes learned from then on.'
        ),
    )

    # Maintenance mode
    maintenance_mode = models.BooleanField(default=False, verbose_name='Maintenance Mode')
    maintenance_enabled_at = models.DateTimeField(null=True, blank=True)
    maintenance_enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    maintenance_notes = models.TextField(blank=True, default='')

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

    TYPE_STRING = DHCPOptionDataTypeChoices.STRING
    TYPE_IP_ADDRESS = DHCPOptionDataTypeChoices.IP_ADDRESS
    TYPE_IP_ADDRESS_LIST = DHCPOptionDataTypeChoices.IP_ADDRESS_LIST
    TYPE_DWORD = DHCPOptionDataTypeChoices.DWORD
    TYPE_DWORD_DWORD = DHCPOptionDataTypeChoices.DWORD_DWORD
    TYPE_BINARY = DHCPOptionDataTypeChoices.BINARY
    TYPE_ENCAPSULATED = DHCPOptionDataTypeChoices.ENCAPSULATED
    TYPE_IPV6_ADDRESS = DHCPOptionDataTypeChoices.IPV6_ADDRESS
    DATA_TYPE_CHOICES = DHCPOptionDataTypeChoices

    code = models.PositiveSmallIntegerField(
        unique=True,
        verbose_name='Option Code',
        help_text='DHCP option code number (1–254)',
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    data_type = models.CharField(
        max_length=20,
        choices=DHCPOptionDataTypeChoices,
        default=DHCPOptionDataTypeChoices.STRING,
    )
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
            raise ProtectedError(
                'Built-in DHCP option code definitions cannot be deleted.',
                set(),
            )
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

    def delete(self, *args, **kwargs):
        # Refused while any scope still uses it (UI, bulk and API all delete one by one).
        from .locks import check_option_value_delete
        check_option_value_delete(self)
        return super().delete(*args, **kwargs)


class DHCPExclusionRange(NetBoxModel):
    """
    An IP address range excluded from dynamic allocation within a DHCP scope.
    Windows DHCP exclusion ranges are identified by scope + start_ip + end_ip;
    there is no server-side ID. Multiple exclusion ranges are allowed per scope.
    """

    scope = models.ForeignKey(
        'DHCPScope',
        on_delete=models.CASCADE,
        related_name='exclusion_ranges',
        verbose_name='Scope',
    )
    start_ip = models.GenericIPAddressField(verbose_name='Start IP')
    end_ip = models.GenericIPAddressField(verbose_name='End IP')
    description = models.CharField(
        max_length=200,
        blank=True,
        help_text='Stored in NetBox only — Windows DHCP exclusions have no description.',
    )

    class Meta:
        ordering = ['scope', 'start_ip']
        unique_together = [('scope', 'start_ip', 'end_ip')]
        verbose_name = 'DHCP Exclusion Range'
        verbose_name_plural = 'DHCP Exclusion Ranges'

    def __str__(self):
        return f'{self.start_ip} – {self.end_ip}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_windows_dhcp:dhcpexclusionrange', args=[self.pk])

    def clean(self):
        super().clean()
        if not self.start_ip or not self.end_ip:
            return
        try:
            start = NetAddrIP(self.start_ip)
            end = NetAddrIP(self.end_ip)
        except Exception:
            return
        if start > end:
            raise ValidationError(
                {'end_ip': 'End IP must be greater than or equal to the Start IP.'}
            )
        if self.scope_id:
            scope_net = self.scope.network_cidr
            if scope_net is None:
                return
            if start not in scope_net:
                raise ValidationError(
                    {'start_ip': f'Start IP must be within the scope network {scope_net}.'}
                )
            if end not in scope_net:
                raise ValidationError(
                    {'end_ip': f'End IP must be within the scope network {scope_net}.'}
                )
        from .locks import check_exclusion_change
        check_exclusion_change(self)

    def delete(self, *args, **kwargs):
        # Only a direct delete — deleting the whole scope cascades past this.
        from .locks import check_exclusion_delete
        check_exclusion_delete(self)
        return super().delete(*args, **kwargs)


class DHCPScope(NetBoxModel):
    """
    A DHCP scope, usually associated with a NetBox Prefix.

    The scope stores its own network and prefix length (copied from the prefix when it
    has one); the scope ID on the Windows server is that network address. A scope with
    no prefix is "settings-only": its name, range, options and exclusions are still
    synced, but nothing that depends on NetBox IPs (IP sync, reservations, locks).
    """

    name = models.CharField(max_length=200)
    description = models.CharField(max_length=200, blank=True)
    prefix = models.ForeignKey(
        'ipam.Prefix',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='dhcp_scopes',
        verbose_name='Prefix',
        help_text='Leave blank for a scope with no prefix (IP sync and reservations are skipped for it).',
    )
    # Optional at the database level: filled in from the prefix in clean()/save(), and
    # required by clean() when there's no prefix.
    network = models.GenericIPAddressField(
        null=True,
        blank=True,
        verbose_name='Network',
        help_text='The scope ID on the DHCP server. Copied from the prefix when one is set.',
    )
    prefix_length = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name='Prefix Length',
        help_text='Copied from the prefix when one is set.',
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
    # The Windows scope state (Active/InActive): pulled while push_scope_info is off,
    # pushed (to the primary, then replicated) while it is on.
    active = models.BooleanField(
        default=True,
        verbose_name='Active',
        help_text='Whether the scope is active on the DHCP server.',
    )
    server = models.ForeignKey(
        DHCPServer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='standalone_scopes',
        verbose_name='Server',
        help_text='For standalone scopes not part of a failover relationship.',
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

    # Maintenance mode
    maintenance_mode = models.BooleanField(default=False, verbose_name='Maintenance Mode')
    maintenance_enabled_at = models.DateTimeField(null=True, blank=True)
    maintenance_enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    maintenance_notes = models.TextField(blank=True, default='')
    last_sync_at = models.DateTimeField(null=True, blank=True, verbose_name='Last Sync')

    class Meta:
        ordering = ['name']
        verbose_name = 'DHCP Scope'
        verbose_name_plural = 'DHCP Scopes'

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_windows_dhcp:dhcpscope', args=[self.pk])

    def serialize_object(self, exclude=None):
        return super().serialize_object(exclude=[*(exclude or []), 'last_sync_at'])

    @property
    def lease_lifetime_display(self) -> str:
        from .utils import lease_lifetime_display
        return lease_lifetime_display(self.lease_lifetime)

    @property
    def network_cidr(self):
        """The scope's subnet (stored network + prefix length) as an IPNetwork, or None."""
        if not self.network or self.prefix_length is None:
            return None
        try:
            return IPNetwork(f'{self.network}/{self.prefix_length}')
        except Exception:
            return None

    def _copy_network_from_prefix(self):
        """Set network and prefix_length from the prefix, when there is one."""
        if not self.prefix_id:
            return
        try:
            prefix_net = IPNetwork(str(self.prefix.prefix))
        except Exception:
            return
        self.network = str(prefix_net.network)
        self.prefix_length = prefix_net.prefixlen

    def save(self, *args, **kwargs):
        self._copy_network_from_prefix()
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'prefix' in update_fields:
            kwargs['update_fields'] = {*update_fields, 'network', 'prefix_length'}
        super().save(*args, **kwargs)

    def _clean_network(self):
        """
        Check the stored network against the prefix (or on its own when there's none),
        then copy it from the prefix. Linking a prefix to a scope that had none needs a
        prefix whose network and length match what the scope already stores.
        """
        stored = None
        if self.pk:
            stored = DHCPScope.objects.filter(pk=self.pk).values(
                'prefix_id', 'network', 'prefix_length',
            ).first()

        if self.prefix_id:
            try:
                prefix_net = IPNetwork(str(self.prefix.prefix))
            except Exception:
                return
            if stored and stored['prefix_id'] is None:
                wanted = (stored['network'], stored['prefix_length'])
                what = 'this scope\'s stored network'
            elif not stored and self.network:
                wanted = (self.network, self.prefix_length)
                what = 'the network entered'
            else:
                wanted = None
            if wanted and wanted != (str(prefix_net.network), prefix_net.prefixlen):
                raise ValidationError({
                    'prefix': f'Prefix {prefix_net} doesn\'t match {what} '
                              f'({wanted[0]}/{wanted[1]}). Pick a prefix with the same '
                              f'network and length.',
                })
            self._copy_network_from_prefix()
            return

        if not self.network or self.prefix_length is None:
            raise ValidationError(
                'Set a prefix, or enter the network and prefix length for a scope with no prefix.'
            )
        try:
            net = IPNetwork(f'{self.network}/{self.prefix_length}')
        except Exception:
            raise ValidationError({'network': f'{self.network}/{self.prefix_length} is not a valid network.'})
        if net.version != 4:
            raise ValidationError({'network': 'Windows DHCP scopes are IPv4 only.'})
        if str(net.network) != str(self.network):
            raise ValidationError({
                'network': f'{self.network} is not the network address of a /{self.prefix_length} '
                           f'(did you mean {net.network}?).',
            })

    def _server_ids(self):
        """The DHCP servers this scope lives on: its own server, or both failover partners."""
        if self.server_id:
            return {self.server_id}
        if self.failover_id:
            return {self.failover.primary_server_id, self.failover.secondary_server_id}
        return set()

    def _clean_uniqueness(self):
        """
        One scope per prefix, and one scope per network on each DHCP server (standalone
        scopes on it plus scopes in any failover it's a member of).
        """
        from django.db.models import Q

        others = DHCPScope.objects.exclude(pk=self.pk) if self.pk else DHCPScope.objects.all()

        if self.prefix_id:
            taken = others.filter(prefix_id=self.prefix_id).first()
            if taken is not None:
                raise ValidationError({
                    'prefix': f'Prefix {self.prefix.prefix} already belongs to scope "{taken.name}". '
                              f'A prefix can have only one scope.',
                })

        server_ids = self._server_ids()
        if not server_ids or not self.network:
            return
        clash = others.filter(network=self.network).filter(
            Q(server_id__in=server_ids)
            | Q(failover__primary_server_id__in=server_ids)
            | Q(failover__secondary_server_id__in=server_ids)
        ).first()
        if clash is not None:
            raise ValidationError(
                f'Scope "{clash.name}" already uses network {self.network} on the same DHCP '
                f'server. A DHCP server can have only one scope per network.'
            )

    def clean(self):
        super().clean()
        has_server = bool(self.server_id)
        has_failover = bool(self.failover_id)
        if has_server and has_failover:
            raise ValidationError(
                'A scope cannot have both a server and a failover relationship. Set only one.'
            )
        if not has_server and not has_failover:
            raise ValidationError(
                'A scope must be associated with either a server or a failover relationship.'
            )

        self._clean_network()
        self._clean_uniqueness()

        scope_net = self.network_cidr
        if scope_net is None or not self.start_ip or not self.end_ip:
            return

        try:
            start = NetAddrIP(self.start_ip)
            end = NetAddrIP(self.end_ip)
        except Exception:
            return

        if start not in scope_net:
            raise ValidationError(
                {'start_ip': f'Start IP must be within the scope network {scope_net}.'}
            )
        if end not in scope_net:
            raise ValidationError(
                {'end_ip': f'End IP must be within the scope network {scope_net}.'}
            )
        if start > end:
            raise ValidationError(
                {'end_ip': 'End IP must be greater than or equal to the Start IP.'}
            )

        from .locks import check_scope_range
        check_scope_range(self)


class DHCPLeaseInfo(models.Model):
    """
    DHCP lease/reservation metadata for a NetBox IPAddress.

    Stored separately from IPAddress to avoid flooding the changelog on every sync.
    Created and updated exclusively by the background sync job.
    The presence of this record marks an IP address as DHCP-managed — used by
    cleanup logic to distinguish plugin-managed 'reserved' IPs from manually-created ones.
    Cascades on IPAddress deletion so it always stays in sync.

    Uses RestrictedQuerySet so NetBox's search backend can call .restrict() when
    resolving search results through its permission system.
    """
    objects = RestrictedQuerySet.as_manager()

    ip_address = models.OneToOneField(
        'ipam.IPAddress',
        on_delete=models.CASCADE,
        related_name='dhcp_lease_info',
    )
    lease_hostname = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='Lease Hostname',
        help_text='Hostname reported by the DHCP server for this lease or reservation.',
    )
    active = models.BooleanField(
        default=False,
        verbose_name='Active',
        help_text='True if this IP was seen as an active lease or reservation on the last sync.',
    )
    lease_expiration = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Lease Expiration',
        help_text='When this lease expires. Null for reservations (they do not expire).',
    )

    class Meta:
        verbose_name = 'DHCP Lease Info'
        verbose_name_plural = 'DHCP Lease Info'

    def __str__(self):
        return self.lease_hostname or f'DHCP info for {self.ip_address}'

    def get_absolute_url(self):
        """Return the IP Address detail URL so search results link to the right page."""
        return self.ip_address.get_absolute_url()
