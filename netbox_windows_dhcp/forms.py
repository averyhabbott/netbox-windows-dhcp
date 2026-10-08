from django import forms
from django.utils.html import format_html

from extras.models import Tag
from ipam.choices import IPAddressStatusChoices
from ipam.models import VRF, Prefix
from tenancy.models import Tenant
from netbox.forms import NetBoxModelBulkEditForm, NetBoxModelForm, NetBoxModelFilterSetForm
from utilities.forms.utils import add_blank_choice
from utilities.forms.fields import (
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
    TagFilterField,
)
from utilities.forms.constants import BOOLEAN_WITH_BLANK_CHOICES
from utilities.forms.rendering import FieldSet, InlineFields
from utilities.forms.widgets import BulkEditNullBooleanSelect, DateTimePicker

from .choices import DHCPOptionDataTypeChoices, DHCPServerAccessChoices, DHCPServerHealthChoices
from .locks import check_option_codes, server_owns_scope_info
from .models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPLeaseInfo,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPPluginSettings,
    DHCPScope,
    DHCPServer,
)


# ---------------------------------------------------------------------------
# DHCPExclusionRange
# ---------------------------------------------------------------------------

class DHCPExclusionRangeForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('scope', 'start_ip', 'end_ip', 'description', name='Exclusion Range'),
        FieldSet('tags', name='Tags'),
    )

    scope = DynamicModelChoiceField(
        queryset=DHCPScope.objects.all(),
        label='Scope',
    )

    #: Shown read-only on an edit while push_scope_info is off (the server owns them).
    READ_ONLY_WHEN_PUSH_OFF = ('scope', 'start_ip', 'end_ip')

    class Meta:
        model = DHCPExclusionRange
        fields = ('scope', 'start_ip', 'end_ip', 'description', 'tags')
        labels = {
            'start_ip': 'Start IP',
            'end_ip': 'End IP',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Pre-populate scope from query param when arriving from the scope detail page
        scope_id = self.initial.get('scope') or self.data.get('scope')
        if scope_id and not self.instance.pk:
            try:
                self.initial['scope'] = int(scope_id)
            except (TypeError, ValueError):
                pass
        # push_scope_info off: the server owns the range, so only the NetBox-only
        # fields stay editable (disabled fields ignore anything posted).
        self.server_owned = bool(self.instance.pk and server_owns_scope_info())
        if self.server_owned:
            for name in self.READ_ONLY_WHEN_PUSH_OFF:
                self.fields[name].disabled = True
                self.fields[name].required = False


# ---------------------------------------------------------------------------
# DHCPServer
# ---------------------------------------------------------------------------

class DHCPServerForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('name', 'hostname', 'port', 'use_https', 'api_key', 'verify_ssl', name='Server'),
        FieldSet('sync_standalone_scopes', 'default_scope_vrf', name='Sync'),
        FieldSet('tags', name='Tags'),
    )

    default_scope_vrf = DynamicModelChoiceField(
        queryset=VRF.objects.all(),
        required=False,
        label='Default Scope VRF',
        help_text=(
            'Where standalone scopes learned from this server look for (or create) their prefix. '
            'Blank means the global VRF. Changing it only affects scopes learned from then on.'
        ),
    )
    ca_cert = forms.CharField(widget=forms.HiddenInput(), required=False)
    ca_cert_expiry = forms.CharField(widget=forms.HiddenInput(), required=False)

    class Meta:
        model = DHCPServer
        fields = (
            'name', 'hostname', 'port', 'use_https', 'api_key', 'verify_ssl',
            'sync_standalone_scopes', 'default_scope_vrf', 'tags', 'ca_cert', 'ca_cert_expiry',
        )
        labels = {
            'api_key': 'App Token',
        }
        widgets = {
            'api_key': forms.PasswordInput(render_value=False),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial['ca_cert'] = self.instance.ca_cert or ''
            expiry = self.instance.ca_cert_expiry
            self.initial['ca_cert_expiry'] = expiry.isoformat() if expiry else ''

    def clean_api_key(self):
        value = self.cleaned_data.get('api_key', '').strip()
        if not value and self.instance.pk:
            return self.instance.api_key
        return value

    def clean_ca_cert(self):
        value = self.cleaned_data.get('ca_cert', '').strip()
        if not value and self.instance.pk:
            return self.instance.ca_cert
        return value

    def clean_ca_cert_expiry(self):
        value = self.cleaned_data.get('ca_cert_expiry', '').strip()
        if not value:
            if self.instance.pk and self.cleaned_data.get('ca_cert') == self.instance.ca_cert:
                return self.instance.ca_cert_expiry
            return None
        from django.utils.dateparse import parse_datetime
        dt = parse_datetime(value)
        if dt is None:
            from datetime import datetime
            try:
                dt = datetime.fromisoformat(value)
            except ValueError:
                return None
        return dt


class DHCPServerFilterForm(NetBoxModelFilterSetForm):
    model = DHCPServer
    health_status = forms.MultipleChoiceField(
        choices=DHCPServerHealthChoices,
        required=False,
        label='Health Status',
    )
    access_level = forms.MultipleChoiceField(
        choices=DHCPServerAccessChoices,
        required=False,
        label='Writable',
    )
    tag = TagFilterField(model)


# ---------------------------------------------------------------------------
# DHCPFailover
# ---------------------------------------------------------------------------

class DHCPFailoverForm(NetBoxModelForm):
    """
    Failovers are managed on the DHCP server and imported. Only the NetBox-side fields
    can be edited here — Default Scope VRF, description and tags; the rest is shown
    read-only (disabled fields ignore anything posted). The shared secret is never shown.
    Sync on/off and maintenance mode live on the failover's own page.
    """
    fieldsets = (
        FieldSet('name', 'description', 'default_scope_vrf', name='Failover'),
        FieldSet('primary_server', 'secondary_server', 'mode', name='Servers (read-only)'),
        FieldSet(
            'max_client_lead_time', 'max_response_delay', 'state_switchover_interval', 'enable_auth',
            name='Settings from the DHCP server (read-only)',
        ),
        FieldSet('tags', name='Tags'),
    )

    #: Fields owned by the DHCP server — shown, never edited.
    READ_ONLY_FIELDS = (
        'name', 'primary_server', 'secondary_server', 'mode', 'max_client_lead_time',
        'max_response_delay', 'state_switchover_interval', 'enable_auth',
    )

    primary_server = DynamicModelChoiceField(
        queryset=DHCPServer.objects.all(),
        required=False,
        label='Primary Server',
    )
    secondary_server = DynamicModelChoiceField(
        queryset=DHCPServer.objects.all(),
        required=False,
        label='Secondary Server',
    )
    default_scope_vrf = DynamicModelChoiceField(
        queryset=VRF.objects.all(),
        required=False,
        label='Default Scope VRF',
        help_text=(
            'Where scopes learned for this failover look for (or create) their prefix. '
            'Blank means the global VRF. Changing it only affects scopes learned from then on.'
        ),
    )

    class Meta:
        model = DHCPFailover
        fields = (
            'name',
            'description',
            'default_scope_vrf',
            'primary_server',
            'secondary_server',
            'mode',
            'max_client_lead_time',
            'max_response_delay',
            'state_switchover_interval',
            'enable_auth',
            'tags',
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.READ_ONLY_FIELDS:
            self.fields[name].disabled = True
            self.fields[name].required = False


class DHCPFailoverFilterForm(NetBoxModelFilterSetForm):
    model = DHCPFailover
    primary_server = DynamicModelChoiceField(
        queryset=DHCPServer.objects.all(),
        required=False,
    )
    secondary_server = DynamicModelChoiceField(
        queryset=DHCPServer.objects.all(),
        required=False,
    )
    tag = TagFilterField(model)


# ---------------------------------------------------------------------------
# DHCPOptionCodeDefinition
# ---------------------------------------------------------------------------

class DHCPOptionCodeDefinitionForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('code', 'name', 'data_type', 'description', 'vendor_class', name='Option Definition'),
        FieldSet('tags', name='Tags'),
    )

    class Meta:
        model = DHCPOptionCodeDefinition
        fields = ('code', 'name', 'data_type', 'description', 'vendor_class', 'tags')


class DHCPOptionCodeDefinitionFilterForm(NetBoxModelFilterSetForm):
    model = DHCPOptionCodeDefinition
    data_type = forms.ChoiceField(
        choices=add_blank_choice(DHCPOptionDataTypeChoices),
        required=False,
        label='Data Type',
    )
    is_builtin = forms.NullBooleanField(
        required=False,
        label='Built-in',
        widget=forms.Select(
            choices=[('', '---------'), ('true', 'Yes'), ('false', 'No')]
        ),
    )
    tag = TagFilterField(model)


# ---------------------------------------------------------------------------
# DHCPOptionValue
# ---------------------------------------------------------------------------

class DHCPOptionValueForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('option_definition', 'value', 'friendly_name', name='Option Value'),
        FieldSet('tags', name='Tags'),
    )

    option_definition = DynamicModelChoiceField(
        queryset=DHCPOptionCodeDefinition.objects.all(),
        label='Option Definition',
    )

    class Meta:
        model = DHCPOptionValue
        fields = ('option_definition', 'value', 'friendly_name', 'tags')


class DHCPOptionValueFilterForm(NetBoxModelFilterSetForm):
    model = DHCPOptionValue
    option_definition = DynamicModelChoiceField(
        queryset=DHCPOptionCodeDefinition.objects.all(),
        required=False,
        label='Option Definition',
    )
    tag = TagFilterField(model)


# ---------------------------------------------------------------------------
# DHCPScope
# ---------------------------------------------------------------------------

LEASE_LIFETIME_UNIT_CHOICES = [
    ('seconds', 'Seconds'),
    ('minutes', 'Minutes'),
    ('hours',   'Hours'),
    ('days',    'Days'),
]

LEASE_LIFETIME_UNIT_MULTIPLIERS = {
    'seconds': 1,
    'minutes': 60,
    'hours':   3600,
    'days':    86400,
}


class DHCPScopeForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('name', 'active', 'description', 'prefix', 'network', 'prefix_length', name='Scope Identity'),
        FieldSet(
            'start_ip', 'end_ip', 'router',
            InlineFields('lease_lifetime_value', 'lease_lifetime_unit', label='Lease Lifetime'),
            name='IP Range',
        ),
        FieldSet('server', 'failover', 'option_values', name='Scope Source & Configuration'),
        FieldSet('tags', name='Tags'),
    )

    prefix = DynamicModelChoiceField(
        queryset=Prefix.objects.all(),
        required=False,
        label='Prefix',
        help_text=(
            'A prefix can have only one scope. Leave blank for a scope with no prefix '
            '(IP sync and reservations are skipped for it) and enter its network below.'
        ),
    )
    network = forms.GenericIPAddressField(
        protocol='IPv4',
        required=False,
        label='Network',
        help_text='Only needed when no prefix is set (the scope ID on the DHCP server, e.g. 10.0.1.0).',
    )
    prefix_length = forms.IntegerField(
        min_value=1,
        max_value=32,
        required=False,
        label='Prefix Length',
        help_text='Only needed when no prefix is set (e.g. 24).',
    )
    server = DynamicModelChoiceField(
        queryset=DHCPServer.objects.all(),
        required=False,
        label='Server',
        help_text='For standalone scopes. Leave blank if this scope uses a failover relationship.',
    )
    failover = DynamicModelChoiceField(
        queryset=DHCPFailover.objects.all(),
        required=False,
        label='Failover Relationship',
        help_text='Leave blank if this scope is standalone (set Server instead).',
    )
    option_values = DynamicModelMultipleChoiceField(
        queryset=DHCPOptionValue.objects.all(),
        required=False,
        label='Option Values',
    )
    lease_lifetime_value = forms.IntegerField(
        min_value=1,
        label='Lease Lifetime',
    )
    lease_lifetime_unit = forms.ChoiceField(
        choices=LEASE_LIFETIME_UNIT_CHOICES,
        label='Unit',
    )

    #: Shown read-only on an edit while push_scope_info is off (the server owns them).
    READ_ONLY_WHEN_PUSH_OFF = (
        'name', 'active', 'description', 'network', 'prefix_length', 'start_ip', 'end_ip', 'router',
        'lease_lifetime_value', 'lease_lifetime_unit', 'server', 'failover', 'option_values',
    )

    class Meta:
        model = DHCPScope
        fields = (
            'name',
            'active',
            'description',
            'prefix',
            'network',
            'prefix_length',
            'start_ip',
            'end_ip',
            'router',
            'server',
            'failover',
            'option_values',
            'tags',
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Pre-populate value+unit from the stored seconds when editing
        if self.instance and self.instance.pk:
            from .utils import decompose_lease_lifetime
            value, unit = decompose_lease_lifetime(self.instance.lease_lifetime)
            self.initial['lease_lifetime_value'] = value
            self.initial['lease_lifetime_unit'] = unit
        else:
            self.initial.setdefault('lease_lifetime_value', 1)
            self.initial.setdefault('lease_lifetime_unit', 'days')
        # A scope with a prefix takes its network from it — shown, not edited.
        if self.instance and self.instance.pk and self.instance.prefix_id:
            for name in ('network', 'prefix_length'):
                self.fields[name].disabled = True
                self.fields[name].help_text = 'Copied from the prefix.'
        # push_scope_info off: the server owns the settings, so only the NetBox-only
        # fields stay editable (disabled fields ignore anything posted).
        self.server_owned = bool(self.instance and self.instance.pk and server_owns_scope_info())
        if self.server_owned:
            for name in self.READ_ONLY_WHEN_PUSH_OFF:
                self.fields[name].disabled = True
                self.fields[name].required = False

    def clean(self):
        cleaned = super().clean() or self.cleaned_data

        # Enforce mutual exclusion: exactly one of server or failover must be set.
        has_server = bool(cleaned.get('server'))
        has_failover = bool(cleaned.get('failover'))
        if has_server and has_failover:
            raise forms.ValidationError(
                'Set either Server or Failover Relationship, not both.'
            )
        if not has_server and not has_failover:
            raise forms.ValidationError(
                'A scope must be associated with either a Server or a Failover Relationship.'
            )

        # No two selected option values may share an option code.
        option_values = cleaned.get('option_values')
        if option_values:
            check_option_codes(option_values)

        value = cleaned.get('lease_lifetime_value')
        unit = cleaned.get('lease_lifetime_unit', 'seconds')
        if value is not None:
            multiplier = LEASE_LIFETIME_UNIT_MULTIPLIERS.get(unit, 1)
            self.instance.lease_lifetime = value * multiplier
        return cleaned


def _get_scope_filter_fields():
    """Return Site, Location, VRF querysets — deferred to avoid circular import at module load."""
    from dcim.models import Location, Site
    from ipam.models import VRF
    return Site.objects.all(), Location.objects.all(), VRF.objects.all()


class DHCPScopeFilterForm(NetBoxModelFilterSetForm):
    model = DHCPScope
    server = DynamicModelChoiceField(
        queryset=DHCPServer.objects.all(),
        required=False,
    )
    failover = DynamicModelChoiceField(
        queryset=DHCPFailover.objects.all(),
        required=False,
    )
    within_prefix = forms.CharField(
        required=False,
        label='Within Prefix',
        widget=forms.TextInput(attrs={'placeholder': '10.0.0.0/8'}),
    )
    has_prefix = forms.NullBooleanField(
        required=False,
        label='Has Prefix',
        widget=forms.Select(
            choices=[('', '---------'), ('true', 'Yes'), ('false', 'No (unassigned)')]
        ),
    )
    active = forms.NullBooleanField(
        required=False,
        label='Active',
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
    )
    maintenance_mode = forms.NullBooleanField(
        required=False,
        label='Maintenance Mode',
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
    )
    lease_lifetime = forms.IntegerField(required=False, label='Lease Lifetime (seconds)')
    tag = TagFilterField(model)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from dcim.models import Location, Site
        from ipam.models import VRF
        self.fields['site'] = DynamicModelMultipleChoiceField(
            queryset=Site.objects.all(), required=False, label='Site',
        )
        self.fields['location'] = DynamicModelMultipleChoiceField(
            queryset=Location.objects.all(), required=False, label='Location',
        )
        self.fields['vrf'] = DynamicModelMultipleChoiceField(
            queryset=VRF.objects.all(), required=False, label='VRF',
        )
        self.fields['tenant'] = DynamicModelMultipleChoiceField(
            queryset=Tenant.objects.all(), required=False, label='Tenant',
        )


class DHCPLeaseFilterForm(NetBoxModelFilterSetForm):
    model = DHCPLeaseInfo
    fieldsets = (
        FieldSet('q', 'filter_id', 'tag'),
        FieldSet('parent', 'status', 'active', 'lease_hostname', 'dns_name', 'client_id', 'description',
                 name='Address'),
        FieldSet('vrf_id', 'tenant_id', name='VRF / Tenant'),
        FieldSet('scope_id', 'server_id', 'failover_id', name='Scope'),
        FieldSet('expiration_after', 'expiration_before', name='Expiration'),
        FieldSet('state_changed_after', 'state_changed_before', name='Active/Inactive Since'),
    )
    parent = forms.CharField(
        required=False,
        label='Parent Prefix',
        widget=forms.TextInput(attrs={'placeholder': 'Prefix'}),
    )
    status = forms.MultipleChoiceField(
        required=False, label='Status', choices=IPAddressStatusChoices,
    )
    active = forms.NullBooleanField(
        required=False,
        label='Active',
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
    )
    lease_hostname = forms.CharField(required=False, label='Lease Hostname')
    dns_name = forms.CharField(required=False, label='DNS Name')
    client_id = forms.CharField(required=False, label='Client ID')
    description = forms.CharField(required=False, label='Description')
    vrf_id = DynamicModelMultipleChoiceField(
        queryset=VRF.objects.all(), required=False, label='VRF', null_option='Global',
    )
    tenant_id = DynamicModelMultipleChoiceField(
        queryset=Tenant.objects.all(), required=False, label='Tenant', null_option='None',
    )
    scope_id = DynamicModelMultipleChoiceField(
        queryset=DHCPScope.objects.all(), required=False, label='Scope',
    )
    server_id = DynamicModelMultipleChoiceField(
        queryset=DHCPServer.objects.all(), required=False, label='Server',
    )
    failover_id = DynamicModelMultipleChoiceField(
        queryset=DHCPFailover.objects.all(), required=False, label='Failover',
    )
    expiration_after = forms.DateTimeField(required=False, label='After', widget=DateTimePicker())
    expiration_before = forms.DateTimeField(required=False, label='Before', widget=DateTimePicker())
    state_changed_after = forms.DateTimeField(required=False, label='After', widget=DateTimePicker())
    state_changed_before = forms.DateTimeField(required=False, label='Before', widget=DateTimePicker())
    tag = DynamicModelMultipleChoiceField(
        queryset=Tag.objects.all(), to_field_name='slug', required=False, label='Tag',
    )


class DHCPScopeBulkEditForm(NetBoxModelBulkEditForm):
    model = DHCPScope

    fieldsets = (
        FieldSet(
            'active',
            'description',
            'router',
            InlineFields('lease_lifetime_value', 'lease_lifetime_unit', label='Lease Lifetime'),
            'server',
            'failover',
            name='Scope',
        ),
        FieldSet('add_option_values', 'remove_option_values', name='Option Values'),
    )

    active = forms.NullBooleanField(
        required=False,
        widget=BulkEditNullBooleanSelect(),
        label='Active',
    )
    description = forms.CharField(
        max_length=200,
        required=False,
        label='Description',
    )
    router = forms.GenericIPAddressField(
        required=False,
        label='Router (Option 3)',
    )
    lease_lifetime_value = forms.IntegerField(
        required=False,
        min_value=1,
        label='Lease Lifetime',
    )
    lease_lifetime_unit = forms.ChoiceField(
        choices=[('', '--------')] + LEASE_LIFETIME_UNIT_CHOICES,
        required=False,
        label='Unit',
    )
    # Hidden field — computed in clean() so BulkEditView can apply it to each object
    lease_lifetime = forms.IntegerField(
        required=False,
        widget=forms.HiddenInput(),
    )

    def clean(self):
        cleaned = super().clean() or self.cleaned_data
        value = cleaned.get('lease_lifetime_value')
        unit = cleaned.get('lease_lifetime_unit')
        if value and unit:
            multiplier = LEASE_LIFETIME_UNIT_MULTIPLIERS.get(unit, 1)
            cleaned['lease_lifetime'] = value * multiplier
        else:
            # Neither provided — don't touch lease_lifetime on any object
            cleaned.pop('lease_lifetime', None)
        return cleaned
    server = DynamicModelChoiceField(
        queryset=DHCPServer.objects.all(),
        required=False,
        label='Server',
    )
    failover = DynamicModelChoiceField(
        queryset=DHCPFailover.objects.all(),
        required=False,
        label='Failover Relationship',
    )
    add_option_values = DynamicModelMultipleChoiceField(
        queryset=DHCPOptionValue.objects.all(),
        required=False,
        label='Add Option Values',
    )
    remove_option_values = DynamicModelMultipleChoiceField(
        queryset=DHCPOptionValue.objects.all(),
        required=False,
        label='Remove Option Values',
    )

    nullable_fields = ('description', 'router', 'server', 'failover')


# ---------------------------------------------------------------------------
# Plugin Settings
# ---------------------------------------------------------------------------

# Maps PLUGINS_CONFIG key → model field name for boolean overrides
_SETTINGS_OVERRIDE_FIELD_MAP = {
    'sync_ips_from_dhcp': 'sync_ip_addresses',
    'push_reservations': 'push_reservations',
    'push_scope_info': 'push_scope_info',
}


# Settings page layout. Each column is a list of cards: (title, description, groups), where
# each group is (subheading or None, field names). The page shows the columns side by side.
_SETTINGS_COLUMNS = (
    (
        (
            'Leases & Reservations',
            'What the sync does with DHCP leases and reservations, and which IP Addresses it leaves alone.',
            (
                ('Pulled from the DHCP server', ('sync_ip_addresses', 'lease_status', 'reservation_status')),
                ('Pushed to the DHCP server', ('push_reservations', 'reservation_placeholders')),
                ('Protected IP Addresses', ('sync_protect_tag', 'sync_protect_update_client_id')),
            ),
        ),
    ),
    (
        (
            'Scopes',
            'How scopes are kept in step between NetBox and the DHCP servers.',
            (
                (None, ('push_scope_info', 'create_missing_prefixes')),
            ),
        ),
        (
            'Global',
            'How and when the background sync runs.',
            (
                ('Sync job', ('sync_interval', 'sync_queue', 'sync_job_timeout', 'sync_log_level')),
                ('REST API', ('api_enabled',)),
            ),
        ),
    ),
)

# Settings whose full explanation is too long to show inline: the short help text is shown
# under the field, and the full explanation opens in a popup from the (?) after it. The
# explanation is the field's model help text unless one is given here.
_SETTINGS_HELP_DETAILS = {
    'push_reservations': (
        'On: NetBox is the source of truth for reservations. Off: the DHCP server is.',
        None,
    ),
    'push_scope_info': (
        'On: NetBox is the source of truth for scopes. Off: the DHCP server is.',
        None,
    ),
    'create_missing_prefixes': (
        'A learned scope whose prefix is not in NetBox gets one, in the Default Scope VRF.',
        None,
    ),
    'reservation_placeholders': (
        'Keeps the DHCP server from handing out reserved IPs that have no client ID.',
        None,
    ),
    'sync_job_timeout': (
        'Maximum seconds a sync job may run before RQ stops it. Increase for servers with large '
        'scope counts.',
        None,
    ),
    'sync_log_level': (
        'Minimum severity written to the job log for DHCP sync/push/delete jobs.',
        None,
    ),
}


class PluginSettingsForm(forms.ModelForm):
    sync_interval = forms.IntegerField(
        min_value=5,
        max_value=1440,
        label='Sync Interval (minutes)',
        help_text='How often the background sync job runs (5–1440 minutes).',
    )
    sync_job_timeout = forms.IntegerField(
        min_value=60,
        label='Sync Job Timeout (seconds)',
        help_text=(
            'Maximum wall-clock seconds a sync job may run before RQ kills it. '
            'Default 300 matches RQ_DEFAULT_TIMEOUT. Increase for servers with '
            'large scope counts. CONN_MAX_AGE is automatically aligned to this '
            'value at job start.'
        ),
    )
    sync_protect_tag = DynamicModelChoiceField(
        queryset=Tag.objects.all(),
        required=False,
        label='Sync-Protected Tag',
        help_text=(
            'IP Addresses carrying this tag, or inside a prefix carrying it, are protected from '
            'being overwritten by a sync: status, DNS name, and the IP itself are never modified '
            'or removed by the sync. The tag does not stop pushes to the DHCP server. '
            'Leave blank to disable.'
        ),
    )
    lease_status = forms.ChoiceField(
        choices=[],
        label='DHCP Lease Status',
    )
    reservation_status = forms.ChoiceField(
        choices=[],
        label='DHCP Reservation Status',
    )

    class Meta:
        model = DHCPPluginSettings
        fields = (
            'lease_status',
            'reservation_status',
            'sync_protect_tag',
            'sync_protect_update_client_id',
            'sync_ip_addresses',
            'push_reservations',
            'reservation_placeholders',
            'push_scope_info',
            'create_missing_prefixes',
            'sync_interval',
            'sync_queue',
            'sync_job_timeout',
            'api_enabled',
            'sync_log_level',
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.overridden_fields = set()

        self.help_details = []
        for name, (short, details) in _SETTINGS_HELP_DETAILS.items():
            field = self.fields[name]
            self.help_details.append(
                (name, field.label, details or DHCPPluginSettings._meta.get_field(name).help_text)
            )
            field.help_text = format_html(
                '{} <a href="#" data-bs-toggle="modal" data-bs-target="#{}_details" '
                'aria-label="{} details"><i class="mdi mdi-help-circle"></i></a>',
                short, name, field.label,
            )

        # Populate status choices from NetBox (includes any custom statuses from FIELD_CHOICES)
        try:
            from ipam.choices import IPAddressStatusChoices
            status_choices = [(v, label) for v, label, *_ in IPAddressStatusChoices.CHOICES]
        except Exception:
            status_choices = [('dhcp', 'DHCP'), ('reserved', 'Reserved')]
        self.fields['lease_status'].choices = status_choices
        self.fields['reservation_status'].choices = status_choices

        try:
            from django.contrib.contenttypes.models import ContentType
            from ipam.models import IPAddress
            ct = ContentType.objects.get_for_model(IPAddress)
            self.fields['sync_protect_tag'].widget.add_query_param('for_object_type_id', ct.pk)
        except Exception:
            pass

        # Disable fields that are overridden by PLUGINS_CONFIG. Django's field.disabled
        # causes the POST value to be ignored and the initial value to be used instead,
        # preserving the DB value regardless of what the browser submits.
        try:
            from django.conf import settings as django_settings
            plugin_cfg = getattr(django_settings, 'PLUGINS_CONFIG', {}).get('netbox_windows_dhcp', {})
            # Need the raw DB values (without overrides) to keep as initial for disabled fields.
            raw_db = None
            if self.instance and self.instance.pk:
                try:
                    raw_db = type(self.instance).objects.get(pk=self.instance.pk)
                except type(self.instance).DoesNotExist:
                    pass
            for cfg_key, field_name in _SETTINGS_OVERRIDE_FIELD_MAP.items():
                if plugin_cfg.get(cfg_key) is not None:
                    self.fields[field_name].disabled = True
                    self.overridden_fields.add(field_name)
                    if raw_db is not None:
                        self.initial[field_name] = getattr(raw_db, field_name)
        except Exception:
            pass

    @property
    def columns(self):
        """
        The settings page layout, as columns of cards: (title, description, groups), where each
        group is (subheading, [(bound field, overridden)]).
        """
        return [
            [
                (title, description, [
                    (heading, [(self[name], name in self.overridden_fields) for name in names])
                    for heading, names in groups
                ])
                for title, description, groups in cards
            ]
            for cards in _SETTINGS_COLUMNS
        ]
