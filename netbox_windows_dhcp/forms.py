from django import forms

from ipam.models import Prefix
from netbox.forms import NetBoxModelBulkEditForm, NetBoxModelForm, NetBoxModelFilterSetForm
from utilities.forms.fields import (
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
    TagFilterField,
)
from utilities.forms.rendering import FieldSet, InlineFields

from .models import (
    DHCPExclusionRange,
    DHCPFailover,
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
        FieldSet('scope', 'start_ip', 'end_ip', name='Exclusion Range'),
        FieldSet('tags', name='Tags'),
    )

    scope = DynamicModelChoiceField(
        queryset=DHCPScope.objects.all(),
        label='Scope',
    )

    class Meta:
        model = DHCPExclusionRange
        fields = ('scope', 'start_ip', 'end_ip', 'tags')
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


# ---------------------------------------------------------------------------
# DHCPServer
# ---------------------------------------------------------------------------

class DHCPServerForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('name', 'hostname', 'port', 'use_https', 'api_key', 'verify_ssl', name='Server'),
        FieldSet('sync_standalone_scopes', name='Sync'),
        FieldSet('tags', name='Tags'),
    )

    class Meta:
        model = DHCPServer
        fields = ('name', 'hostname', 'port', 'use_https', 'api_key', 'verify_ssl', 'sync_standalone_scopes', 'tags')
        labels = {
            'api_key': 'App Token',
        }
        widgets = {
            'api_key': forms.PasswordInput(render_value=True),
        }

    def clean_api_key(self):
        """Strip whitespace that may have been introduced by copy-pasting the token."""
        return self.cleaned_data.get('api_key', '').strip()


class DHCPServerFilterForm(NetBoxModelFilterSetForm):
    model = DHCPServer
    tag = TagFilterField(model)


# ---------------------------------------------------------------------------
# DHCPFailover
# ---------------------------------------------------------------------------

class DHCPFailoverForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('name', 'primary_server', 'secondary_server', 'mode', name='Failover'),
        FieldSet('max_client_lead_time', 'max_response_delay', 'state_switchover_interval', name='Timing'),
        FieldSet('enable_auth', 'shared_secret', name='Authentication'),
        FieldSet('sync_enabled', name='Sync'),
        FieldSet('tags', name='Tags'),
    )

    primary_server = DynamicModelChoiceField(
        queryset=DHCPServer.objects.all(),
        label='Primary Server',
    )
    secondary_server = DynamicModelChoiceField(
        queryset=DHCPServer.objects.all(),
        label='Secondary Server',
    )

    class Meta:
        model = DHCPFailover
        fields = (
            'name',
            'primary_server',
            'secondary_server',
            'mode',
            'max_client_lead_time',
            'max_response_delay',
            'state_switchover_interval',
            'sync_enabled',
            'enable_auth',
            'shared_secret',
            'tags',
        )
        widgets = {
            'shared_secret': forms.PasswordInput(render_value=True),
        }


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
        choices=[('', '---------')] + DHCPOptionCodeDefinition.DATA_TYPE_CHOICES,
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
        FieldSet('name', 'prefix', name='Scope Identity'),
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
        label='Prefix',
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

    class Meta:
        model = DHCPScope
        fields = (
            'name',
            'prefix',
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

        # Validate that no two selected option values share the same option code.
        option_values = cleaned.get('option_values')
        if option_values:
            seen_codes = {}
            duplicates = []
            for ov in option_values:
                code = ov.option_definition.code
                if code in seen_codes:
                    duplicates.append(code)
                else:
                    seen_codes[code] = ov
            if duplicates:
                codes_str = ', '.join(str(c) for c in sorted(set(duplicates)))
                raise forms.ValidationError(
                    f'A scope cannot have more than one value for the same option code. '
                    f'Duplicate code(s): {codes_str}.'
                )

        value = cleaned.get('lease_lifetime_value')
        unit = cleaned.get('lease_lifetime_unit', 'seconds')
        if value is not None:
            multiplier = LEASE_LIFETIME_UNIT_MULTIPLIERS.get(unit, 1)
            self.instance.lease_lifetime = value * multiplier
        return cleaned


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
    tag = TagFilterField(model)


class DHCPScopeBulkEditForm(NetBoxModelBulkEditForm):
    model = DHCPScope

    fieldsets = (
        FieldSet(
            'router',
            InlineFields('lease_lifetime_value', 'lease_lifetime_unit', label='Lease Lifetime'),
            'server',
            'failover',
            name='Scope',
        ),
        FieldSet('add_option_values', 'remove_option_values', name='Option Values'),
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

    nullable_fields = ('router', 'server', 'failover')


# ---------------------------------------------------------------------------
# Plugin Settings
# ---------------------------------------------------------------------------

class PluginSettingsForm(forms.ModelForm):
    sync_interval = forms.IntegerField(
        min_value=5,
        max_value=1440,
        label='Sync Interval (minutes)',
        help_text='How often the background sync job runs (5–1440 minutes).',
    )

    class Meta:
        model = DHCPPluginSettings
        fields = ('sync_ip_addresses', 'push_reservations', 'push_scope_info', 'sync_interval')
