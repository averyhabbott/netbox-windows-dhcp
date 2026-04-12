from django import forms

from ipam.models import Prefix
from netbox.forms import NetBoxModelBulkEditForm, NetBoxModelForm, NetBoxModelFilterSetForm
from utilities.forms.fields import (
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
    TagFilterField,
)
from utilities.forms.rendering import FieldSet

from .models import (
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPPluginSettings,
    DHCPScope,
    DHCPServer,
)


# ---------------------------------------------------------------------------
# DHCPServer
# ---------------------------------------------------------------------------

class DHCPServerForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('name', 'hostname', 'port', 'use_https', 'api_key', 'verify_ssl', name='Server'),
        FieldSet('tags', name='Tags'),
    )

    class Meta:
        model = DHCPServer
        fields = ('name', 'hostname', 'port', 'use_https', 'api_key', 'verify_ssl', 'tags')
        labels = {
            'api_key': 'App Token',
        }
        widgets = {
            'api_key': forms.PasswordInput(render_value=True),
        }


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

class DHCPScopeForm(NetBoxModelForm):
    fieldsets = (
        FieldSet('name', 'prefix', name='Scope Identity'),
        FieldSet('start_ip', 'end_ip', 'router', 'lease_lifetime', name='IP Range'),
        FieldSet('failover', 'option_values', name='DHCP Configuration'),
        FieldSet('tags', name='Tags'),
    )

    prefix = DynamicModelChoiceField(
        queryset=Prefix.objects.all(),
        label='Prefix',
    )
    failover = DynamicModelChoiceField(
        queryset=DHCPFailover.objects.all(),
        required=False,
        label='Failover Relationship',
    )
    option_values = DynamicModelMultipleChoiceField(
        queryset=DHCPOptionValue.objects.all(),
        required=False,
        label='Option Values',
    )

    class Meta:
        model = DHCPScope
        fields = (
            'name',
            'prefix',
            'start_ip',
            'end_ip',
            'router',
            'lease_lifetime',
            'failover',
            'option_values',
            'tags',
        )


class DHCPScopeFilterForm(NetBoxModelFilterSetForm):
    model = DHCPScope
    failover = DynamicModelChoiceField(
        queryset=DHCPFailover.objects.all(),
        required=False,
    )
    tag = TagFilterField(model)


class DHCPScopeBulkEditForm(NetBoxModelBulkEditForm):
    model = DHCPScope

    fieldsets = (
        FieldSet('router', 'lease_lifetime', 'failover', name='Scope'),
        FieldSet('add_option_values', 'remove_option_values', name='Option Values'),
    )

    router = forms.GenericIPAddressField(
        required=False,
        label='Router (Option 3)',
    )
    lease_lifetime = forms.IntegerField(
        required=False,
        min_value=1,
        label='Lease Lifetime (s)',
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

    nullable_fields = ('router', 'failover')


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
        fields = ('scope_sync_mode', 'push_reservations', 'push_scope_info', 'sync_interval')
