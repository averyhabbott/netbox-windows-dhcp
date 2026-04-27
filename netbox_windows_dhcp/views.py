import json
import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from netbox.object_actions import BulkDelete, BulkExport, CloneObject, DeleteObject
from netbox.views import generic

from .filtersets import (
    DHCPExclusionRangeFilterSet,
    DHCPFailoverFilterSet,
    DHCPOptionCodeDefinitionFilterSet,
    DHCPOptionValueFilterSet,
    DHCPScopeFilterSet,
    DHCPServerFilterSet,
)
from .forms import (
    DHCPExclusionRangeForm,
    DHCPFailoverFilterForm,
    DHCPFailoverForm,
    DHCPOptionCodeDefinitionFilterForm,
    DHCPOptionCodeDefinitionForm,
    DHCPOptionValueFilterForm,
    DHCPOptionValueForm,
    DHCPScopeBulkEditForm,
    DHCPScopeFilterForm,
    DHCPScopeForm,
    DHCPServerFilterForm,
    DHCPServerForm,
    PluginSettingsForm,
)
from .models import (
    DHCPExclusionRange,
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)
from .tables import (
    DHCPExclusionRangeTable,
    DHCPFailoverTable,
    DHCPOptionCodeDefinitionTable,
    DHCPOptionValueTable,
    DHCPScopeTable,
    DHCPServerTable,
)

logger = logging.getLogger('netbox_windows_dhcp')


def _setting(name: str):
    """Return the named DHCPPluginSettings boolean, cached per call."""
    from .models import DHCPPluginSettings
    return getattr(DHCPPluginSettings.load(), name)


def _cert_cn_from_pem(pem: str) -> str:
    """Extract subject CN from a PEM string. Returns '' on any failure."""
    if not pem:
        return ''
    try:
        import ssl
        der = ssl.PEM_cert_to_DER_cert(pem)
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        cert = x509.load_der_x509_certificate(der)
        return next(
            (attr.value for attr in cert.subject if attr.oid == NameOID.COMMON_NAME), ''
        )
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# DHCPServer views
# ---------------------------------------------------------------------------

class DHCPServerListView(generic.ObjectListView):
    queryset = DHCPServer.objects.all()
    table = DHCPServerTable
    filterset = DHCPServerFilterSet
    filterset_form = DHCPServerFilterForm
    template_name = 'netbox_windows_dhcp/dhcpserver_list.html'


class DHCPServerView(generic.ObjectView):
    queryset = DHCPServer.objects.all()

    def get_extra_context(self, request, instance):
        from .tables import DHCPFailoverTable as FT
        related_failovers = DHCPFailover.objects.filter(
            primary_server=instance,
        ) | DHCPFailover.objects.filter(secondary_server=instance)
        failover_table = FT(related_failovers)
        failover_table.configure(request)

        from django.db.models import Q
        related_scopes = DHCPScope.objects.filter(
            Q(server=instance) | Q(failover__in=related_failovers)
        )
        scope_table = DHCPScopeTable(related_scopes)
        scope_table.configure(request)

        from django.conf import settings as django_settings
        server_overrides = (
            getattr(django_settings, 'PLUGINS_CONFIG', {})
            .get('netbox_windows_dhcp', {})
            .get('server_overrides', {})
        )

        cert_expired = False
        cert_expiring_soon = False
        if instance.ca_cert_expiry:
            from datetime import timedelta
            from django.utils import timezone
            now = timezone.now()
            cert_expired = instance.ca_cert_expiry < now
            cert_expiring_soon = not cert_expired and instance.ca_cert_expiry < now + timedelta(days=90)

        stored_cert_cn = _cert_cn_from_pem(instance.ca_cert) if instance.ca_cert else ''

        return {
            'failover_table': failover_table,
            'scope_table': scope_table,
            'has_credential_override': instance.hostname in server_overrides,
            'cert_expired': cert_expired,
            'cert_expiring_soon': cert_expiring_soon,
            'stored_cert_cn': stored_cert_cn,
        }


class DHCPServerCreateView(generic.ObjectEditView):
    queryset = DHCPServer.objects.all()
    form = DHCPServerForm
    template_name = 'netbox_windows_dhcp/dhcpserver_edit.html'

    def get_extra_context(self, request, instance):
        from django.urls import reverse
        return {
            'cert_fetch_url': reverse('plugins:netbox_windows_dhcp:dhcpserver_cert_fetch'),
            'test_connection_url': reverse('plugins:netbox_windows_dhcp:dhcpserver_test_connection_new'),
            'stored_cert_cn': '',
            'stored_cert_expiry': '',
            'has_api_key_override': False,
        }


class DHCPServerEditView(generic.ObjectEditView):
    queryset = DHCPServer.objects.all()
    form = DHCPServerForm
    template_name = 'netbox_windows_dhcp/dhcpserver_edit.html'

    def get_extra_context(self, request, instance):
        from django.conf import settings as django_settings
        from django.urls import reverse
        stored_cn = ''
        stored_expiry = ''
        if instance.pk and instance.ca_cert:
            stored_cn = _cert_cn_from_pem(instance.ca_cert)
            if instance.ca_cert_expiry:
                stored_expiry = instance.ca_cert_expiry.strftime('%B %-d, %Y')
        has_api_key_override = bool(
            instance.hostname and
            getattr(django_settings, 'PLUGINS_CONFIG', {})
            .get('netbox_windows_dhcp', {})
            .get('server_overrides', {})
            .get(instance.hostname, {})
            .get('api_key')
        )
        return {
            'cert_fetch_url': reverse('plugins:netbox_windows_dhcp:dhcpserver_cert_fetch'),
            'test_connection_url': reverse(
                'plugins:netbox_windows_dhcp:dhcpserver_test_connection',
                args=[instance.pk],
            ),
            'stored_cert_cn': stored_cn,
            'stored_cert_expiry': stored_expiry,
            'has_api_key_override': has_api_key_override,
        }


class DHCPServerDeleteView(generic.ObjectDeleteView):
    queryset = DHCPServer.objects.all()


class DHCPServerBulkDeleteView(generic.BulkDeleteView):
    queryset = DHCPServer.objects.all()
    table = DHCPServerTable


class DHCPServerSyncView(LoginRequiredMixin, View):
    """Enqueues a background sync job for a single DHCP server."""

    def get(self, request, pk):
        return self._enqueue(request, pk)

    def post(self, request, pk):
        return self._enqueue(request, pk)

    def _enqueue(self, request, pk):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpserver'):
            messages.error(request, 'You do not have permission to sync DHCP servers.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        server = get_object_or_404(DHCPServer, pk=pk)
        if server.maintenance_mode:
            messages.warning(
                request,
                f'"{server.name}" is in maintenance mode. Disable maintenance mode before syncing.'
            )
            return redirect(server.get_absolute_url())
        from .background_tasks import DHCPServerSyncJob
        from .models import DHCPPluginSettings
        cfg = DHCPPluginSettings.load()
        job = DHCPServerSyncJob.enqueue(
            name=f'Sync {server.name}',
            user=request.user,
            server_pk=server.pk,
            queue_name=cfg.sync_queue,
        )
        messages.success(request, f'Sync job queued for {server.name}.')
        return redirect(job.get_absolute_url())


class DHCPGlobalSyncView(LoginRequiredMixin, View):
    """Enqueues a background sync job for every configured DHCP server."""

    def post(self, request):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpserver'):
            messages.error(request, 'You do not have permission to sync DHCP servers.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        from .background_tasks import DHCPServerSyncJob
        from .models import DHCPPluginSettings
        cfg = DHCPPluginSettings.load()
        servers = DHCPServer.objects.all()
        count = 0
        for server in servers:
            DHCPServerSyncJob.enqueue(
                name=f'Sync {server.name}',
                user=request.user,
                server_pk=server.pk,
                queue_name=cfg.sync_queue,
            )
            count += 1
        messages.success(request, f'Queued sync job for {count} server(s). Check System → Jobs for progress.')
        return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')


class DHCPServerImportView(LoginRequiredMixin, View):
    """Enqueues a background import job for a DHCP server."""

    template_name = 'netbox_windows_dhcp/dhcpserver_import.html'

    def _check_permission(self, request):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpserver'):
            messages.error(request, 'You do not have permission to import from DHCP servers.')
            return False
        return True

    def get(self, request, pk):
        if not self._check_permission(request):
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        server = get_object_or_404(DHCPServer, pk=pk)
        return render(request, self.template_name, {'object': server})

    def post(self, request, pk):
        if not self._check_permission(request):
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        server = get_object_or_404(DHCPServer, pk=pk)
        from .background_tasks import DHCPImportJob
        from .models import DHCPPluginSettings
        cfg = DHCPPluginSettings.load()
        job = DHCPImportJob.enqueue(
            name=f'Import from {server.name}',
            user=request.user,
            server_pk=server.pk,
            queue_name=cfg.sync_queue,
        )
        messages.success(request, f'Import job queued for {server.name}.')
        return redirect(job.get_absolute_url())


class DHCPServerCertImportView(LoginRequiredMixin, View):
    """Fetch and trust a TLS certificate from a PSU server (TOFU model)."""

    template_name = 'netbox_windows_dhcp/dhcpserver_certimport.html'

    def _check_permission(self, request):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpserver'):
            messages.error(request, 'You do not have permission to manage DHCP server certificates.')
            return False
        return True

    def get(self, request, pk):
        if not self._check_permission(request):
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        server = get_object_or_404(DHCPServer, pk=pk)
        if not server.use_https:
            messages.error(request, 'Certificate import is only available for HTTPS servers.')
            return redirect(server.get_absolute_url())
        import ssl
        from .cert_utils import fetch_cert_info
        try:
            cert_info = fetch_cert_info(server.hostname, server.port)
        except (ssl.SSLError, OSError) as exc:
            messages.error(request, f'Could not fetch certificate from {server.hostname}:{server.port}: {exc}')
            return redirect(server.get_absolute_url())
        except Exception as exc:
            messages.error(request, f'Unexpected error fetching certificate: {exc}')
            return redirect(server.get_absolute_url())
        return render(request, self.template_name, {
            'object': server,
            'cert_info': cert_info,
        })

    def post(self, request, pk):
        if not self._check_permission(request):
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        server = get_object_or_404(DHCPServer, pk=pk)
        import ssl
        from .cert_utils import fetch_cert_info
        try:
            cert_info = fetch_cert_info(server.hostname, server.port)
        except (ssl.SSLError, OSError) as exc:
            messages.error(request, f'Could not fetch certificate: {exc}')
            return redirect(server.get_absolute_url())
        except Exception as exc:
            messages.error(request, f'Unexpected error fetching certificate: {exc}')
            return redirect(server.get_absolute_url())
        server.ca_cert = cert_info['pem']
        server.ca_cert_expiry = cert_info['not_after']
        server.save(update_fields=['ca_cert', 'ca_cert_expiry'])
        messages.success(
            request,
            f'Certificate imported for {server.name} (expires {cert_info["not_after"].date()}).',
        )
        return redirect(server.get_absolute_url())


class DHCPServerCertRemoveView(LoginRequiredMixin, View):
    """Remove a stored CA certificate from a DHCP server."""

    def post(self, request, pk):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpserver'):
            messages.error(request, 'You do not have permission to manage DHCP server certificates.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        server = get_object_or_404(DHCPServer, pk=pk)
        server.ca_cert = ''
        server.ca_cert_expiry = None
        server.save(update_fields=['ca_cert', 'ca_cert_expiry'])
        messages.success(request, f'Certificate removed from {server.name}.')
        return redirect(server.get_absolute_url())


# ---------------------------------------------------------------------------
# DHCPFailover views
# ---------------------------------------------------------------------------

class DHCPFailoverListView(generic.ObjectListView):
    queryset = DHCPFailover.objects.select_related('primary_server', 'secondary_server')
    table = DHCPFailoverTable
    filterset = DHCPFailoverFilterSet
    filterset_form = DHCPFailoverFilterForm
    template_name = 'netbox_windows_dhcp/dhcpfailover_list.html'
    # No add or bulk_edit — failovers are import-only; sync is toggled via dedicated action
    actions = (BulkExport, BulkDelete)


class DHCPFailoverView(generic.ObjectView):
    queryset = DHCPFailover.objects.select_related('primary_server', 'secondary_server')
    actions = (DeleteObject,)

    def get_extra_context(self, request, instance):
        scope_table = DHCPScopeTable(instance.scopes.all())
        scope_table.configure(request)
        return {'scope_table': scope_table}


class DHCPFailoverCreateView(generic.ObjectEditView):
    queryset = DHCPFailover.objects.all()
    form = DHCPFailoverForm

    def dispatch(self, request, *args, **kwargs):
        messages.error(
            request,
            'Failover relationships are read-only. Import them from a DHCP server to create them.',
        )
        return redirect('plugins:netbox_windows_dhcp:dhcpfailover_list')


class DHCPFailoverEditView(generic.ObjectEditView):
    queryset = DHCPFailover.objects.all()
    form = DHCPFailoverForm


class DHCPFailoverDeleteView(generic.ObjectDeleteView):
    queryset = DHCPFailover.objects.all()


class DHCPFailoverBulkDeleteView(generic.BulkDeleteView):
    queryset = DHCPFailover.objects.all()
    table = DHCPFailoverTable


class DHCPFailoverToggleSyncView(LoginRequiredMixin, View):
    """Toggle sync_enabled on a single failover relationship."""

    def post(self, request, pk):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpfailover'):
            messages.error(request, 'You do not have permission to modify failover sync settings.')
            return redirect('plugins:netbox_windows_dhcp:dhcpfailover_list')
        failover = get_object_or_404(DHCPFailover, pk=pk)
        failover.sync_enabled = not failover.sync_enabled
        failover.save(update_fields=['sync_enabled'])
        state = 'enabled' if failover.sync_enabled else 'disabled'
        messages.success(request, f'Sync {state} for failover "{failover}".')
        return redirect(request.META.get('HTTP_REFERER', 'plugins:netbox_windows_dhcp:dhcpfailover_list'))


class DHCPFailoverBulkToggleSyncView(LoginRequiredMixin, View):
    """Toggle sync_enabled on all selected failover relationships."""

    def post(self, request):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpfailover'):
            messages.error(request, 'You do not have permission to modify failover sync settings.')
            return redirect('plugins:netbox_windows_dhcp:dhcpfailover_list')
        pk_list = [int(pk) for pk in request.POST.getlist('pk') if str(pk).isdigit()]
        if not pk_list:
            messages.warning(request, 'No failover relationships selected.')
            return redirect('plugins:netbox_windows_dhcp:dhcpfailover_list')

        failovers = DHCPFailover.objects.filter(pk__in=pk_list)
        for failover in failovers:
            failover.sync_enabled = not failover.sync_enabled
            failover.save(update_fields=['sync_enabled'])

        messages.success(request, f'Toggled sync for {failovers.count()} failover relationship(s).')
        return redirect(request.POST.get('return_url', 'plugins:netbox_windows_dhcp:dhcpfailover_list'))


# ---------------------------------------------------------------------------
# DHCPOptionCodeDefinition views
# ---------------------------------------------------------------------------

class DHCPOptionCodeDefinitionListView(generic.ObjectListView):
    queryset = DHCPOptionCodeDefinition.objects.all()
    table = DHCPOptionCodeDefinitionTable
    filterset = DHCPOptionCodeDefinitionFilterSet
    filterset_form = DHCPOptionCodeDefinitionFilterForm


class DHCPOptionCodeDefinitionView(generic.ObjectView):
    queryset = DHCPOptionCodeDefinition.objects.all()

    def get_extra_context(self, request, instance):
        value_table = DHCPOptionValueTable(instance.values.all())
        value_table.configure(request)
        return {'value_table': value_table}


class DHCPOptionCodeDefinitionCreateView(generic.ObjectEditView):
    queryset = DHCPOptionCodeDefinition.objects.all()
    form = DHCPOptionCodeDefinitionForm


class DHCPOptionCodeDefinitionEditView(generic.ObjectEditView):
    queryset = DHCPOptionCodeDefinition.objects.all()
    form = DHCPOptionCodeDefinitionForm


class DHCPOptionCodeDefinitionDeleteView(generic.ObjectDeleteView):
    queryset = DHCPOptionCodeDefinition.objects.all()


class DHCPOptionCodeDefinitionBulkDeleteView(generic.BulkDeleteView):
    queryset = DHCPOptionCodeDefinition.objects.all()
    table = DHCPOptionCodeDefinitionTable


# ---------------------------------------------------------------------------
# DHCPOptionValue views
# ---------------------------------------------------------------------------

class DHCPOptionValueListView(generic.ObjectListView):
    queryset = DHCPOptionValue.objects.select_related('option_definition')
    table = DHCPOptionValueTable
    filterset = DHCPOptionValueFilterSet
    filterset_form = DHCPOptionValueFilterForm


class DHCPOptionValueView(generic.ObjectView):
    queryset = DHCPOptionValue.objects.select_related('option_definition')

    def get_extra_context(self, request, instance):
        scope_table = DHCPScopeTable(instance.scopes.all())
        scope_table.configure(request)
        return {'scope_table': scope_table}


class DHCPOptionValueCreateView(generic.ObjectEditView):
    queryset = DHCPOptionValue.objects.all()
    form = DHCPOptionValueForm


class DHCPOptionValueEditView(generic.ObjectEditView):
    queryset = DHCPOptionValue.objects.all()
    form = DHCPOptionValueForm


class DHCPOptionValueDeleteView(generic.ObjectDeleteView):
    queryset = DHCPOptionValue.objects.all()


class DHCPOptionValueBulkDeleteView(generic.BulkDeleteView):
    queryset = DHCPOptionValue.objects.all()
    table = DHCPOptionValueTable


# ---------------------------------------------------------------------------
# DHCPScope views
# ---------------------------------------------------------------------------

class DHCPScopeListView(generic.ObjectListView):
    queryset = DHCPScope.objects.select_related('prefix', 'server', 'failover')
    table = DHCPScopeTable
    filterset = DHCPScopeFilterSet
    filterset_form = DHCPScopeFilterForm
    template_name = 'netbox_windows_dhcp/dhcpscope_list.html'

    def get_extra_context(self, request):
        return {'push_scope_info': _setting('push_scope_info')}


class DHCPScopeView(generic.ObjectView):
    queryset = DHCPScope.objects.select_related('prefix', 'server', 'failover').prefetch_related(
        'option_values__option_definition',
        'exclusion_ranges',
    )

    def get_extra_context(self, request, instance):
        from django.db.models import BooleanField, Q
        from django.db.models.expressions import RawSQL
        from ipam.models import IPAddress
        from ipam.tables import IPAddressTable

        option_table = DHCPOptionValueTable(instance.option_values.all())
        option_table.configure(request)

        exclusion_table = DHCPExclusionRangeTable(instance.exclusion_ranges.all())
        exclusion_table.configure(request)

        # IPs in the dynamic range (start_ip–end_ip) OR within the prefix with dhcp-* status.
        # The range condition uses a RawSQL annotation because Django has no built-in inet
        # range lookup; host(address)::inet strips the prefix length for a pure IP comparison.
        prefix_cidr = str(instance.prefix.prefix)
        from .models import DHCPPluginSettings
        _s = DHCPPluginSettings.load()
        ip_qs = IPAddress.objects.annotate(
            _in_dynamic_range=RawSQL(
                "host(address)::inet >= %s::inet AND host(address)::inet <= %s::inet",
                (instance.start_ip, instance.end_ip),
                output_field=BooleanField(),
            )
        ).filter(
            Q(_in_dynamic_range=True)
            | Q(address__net_contained_or_equal=prefix_cidr, status=_s.lease_status)
            | Q(address__net_contained_or_equal=prefix_cidr, status=_s.reservation_status,
                dhcp_lease_info__isnull=False)
        ).order_by('address')
        ip_table = IPAddressTable(ip_qs)
        ip_table.configure(request)

        return {
            'option_table': option_table,
            'exclusion_table': exclusion_table,
            'ip_table': ip_table,
            'push_scope_info': _setting('push_scope_info'),
        }


_SCOPE_READONLY_MSG = (
    'DHCP Scopes are read-only when "Push Scope Info" is disabled. '
    'Enable it in plugin settings to manage scopes from NetBox.'
)


class DHCPScopeCreateView(generic.ObjectEditView):
    queryset = DHCPScope.objects.all()
    form = DHCPScopeForm

    def dispatch(self, request, *args, **kwargs):
        if not _setting('push_scope_info'):
            messages.error(request, _SCOPE_READONLY_MSG)
            return redirect('plugins:netbox_windows_dhcp:dhcpscope_list')
        return super().dispatch(request, *args, **kwargs)


class DHCPScopeEditView(generic.ObjectEditView):
    queryset = DHCPScope.objects.all()
    form = DHCPScopeForm

    def dispatch(self, request, *args, **kwargs):
        if not _setting('push_scope_info'):
            messages.error(request, _SCOPE_READONLY_MSG)
            return redirect('plugins:netbox_windows_dhcp:dhcpscope_list')
        return super().dispatch(request, *args, **kwargs)


class DHCPScopeDeleteView(generic.ObjectDeleteView):
    queryset = DHCPScope.objects.all()

    def dispatch(self, request, *args, **kwargs):
        if not _setting('push_scope_info'):
            messages.error(request, _SCOPE_READONLY_MSG)
            return redirect('plugins:netbox_windows_dhcp:dhcpscope_list')
        return super().dispatch(request, *args, **kwargs)


class DHCPScopeBulkEditView(generic.BulkEditView):
    queryset = DHCPScope.objects.select_related('prefix', 'failover')
    filterset = DHCPScopeFilterSet
    table = DHCPScopeTable
    form = DHCPScopeBulkEditForm

    def dispatch(self, request, *args, **kwargs):
        if not _setting('push_scope_info'):
            messages.error(request, _SCOPE_READONLY_MSG)
            return redirect('plugins:netbox_windows_dhcp:dhcpscope_list')
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)

        # After the parent saves scalar fields and tags, apply M2M option_values.
        # Only do this when the form was submitted (_apply) and the parent succeeded (redirect).
        if '_apply' in request.POST:
            from django.http import HttpResponseRedirect
            if isinstance(response, HttpResponseRedirect):
                pk_list = [int(pk) for pk in request.POST.getlist('pk') if str(pk).isdigit()]
                form = self.form(data=request.POST, initial={'pk': pk_list})
                if form.is_valid():
                    add_opts = form.cleaned_data.get('add_option_values') or []
                    remove_opts = form.cleaned_data.get('remove_option_values') or []
                    if add_opts or remove_opts:
                        for obj in DHCPScope.objects.filter(pk__in=pk_list):
                            if add_opts:
                                obj.option_values.add(*add_opts)
                            if remove_opts:
                                obj.option_values.remove(*remove_opts)

        return response


class DHCPScopeBulkDeleteView(generic.BulkDeleteView):
    queryset = DHCPScope.objects.all()
    table = DHCPScopeTable

    def dispatch(self, request, *args, **kwargs):
        if not _setting('push_scope_info'):
            messages.error(request, _SCOPE_READONLY_MSG)
            return redirect('plugins:netbox_windows_dhcp:dhcpscope_list')
        return super().dispatch(request, *args, **kwargs)


# ---------------------------------------------------------------------------
# DHCPExclusionRange views
# ---------------------------------------------------------------------------

class DHCPExclusionRangeView(generic.ObjectView):
    queryset = DHCPExclusionRange.objects.select_related('scope__prefix')


class DHCPExclusionRangeCreateView(generic.ObjectEditView):
    queryset = DHCPExclusionRange.objects.all()
    form = DHCPExclusionRangeForm

    def get_extra_addanother_params(self, request):
        # Preserve scope_id on "add another" so the scope stays pre-filled
        return {'scope': request.GET.get('scope', '')}


class DHCPExclusionRangeEditView(generic.ObjectEditView):
    queryset = DHCPExclusionRange.objects.all()
    form = DHCPExclusionRangeForm


class DHCPExclusionRangeDeleteView(generic.ObjectDeleteView):
    queryset = DHCPExclusionRange.objects.all()


# ---------------------------------------------------------------------------
# Maintenance mode — shared helper
# ---------------------------------------------------------------------------

def _apply_maintenance(obj, enabled: bool, notes: str, user):
    from django.utils import timezone
    obj.maintenance_mode = enabled
    if enabled:
        obj.maintenance_notes = notes
        obj.maintenance_enabled_at = timezone.now()
        obj.maintenance_enabled_by = user
    else:
        obj.maintenance_notes = ''
        obj.maintenance_enabled_at = None
        obj.maintenance_enabled_by = None
    obj.save(update_fields=[
        'maintenance_mode', 'maintenance_notes',
        'maintenance_enabled_at', 'maintenance_enabled_by',
    ])


# ---------------------------------------------------------------------------
# Maintenance mode — single-item toggle views
# ---------------------------------------------------------------------------

class DHCPServerMaintenanceView(LoginRequiredMixin, View):
    def get(self, request, pk):
        server = get_object_or_404(DHCPServer, pk=pk)
        return render(request, 'netbox_windows_dhcp/dhcpmaintenance_toggle.html', {
            'object': server,
            'object_type': 'Server',
            'default_enabled': not server.maintenance_mode,
            'return_url': server.get_absolute_url(),
        })

    def post(self, request, pk):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpserver'):
            messages.error(request, 'You do not have permission to modify server maintenance settings.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        server = get_object_or_404(DHCPServer, pk=pk)
        enabled = request.POST.get('maintenance_mode') == '1'
        notes = request.POST.get('maintenance_notes', '')
        _apply_maintenance(server, enabled, notes, request.user)
        action = 'enabled' if enabled else 'disabled'
        messages.success(request, f'Maintenance mode {action} for server "{server}".')
        return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')


class DHCPFailoverMaintenanceView(LoginRequiredMixin, View):
    def get(self, request, pk):
        failover = get_object_or_404(DHCPFailover, pk=pk)
        return render(request, 'netbox_windows_dhcp/dhcpmaintenance_toggle.html', {
            'object': failover,
            'object_type': 'Failover',
            'default_enabled': not failover.maintenance_mode,
            'return_url': failover.get_absolute_url(),
        })

    def post(self, request, pk):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpfailover'):
            messages.error(request, 'You do not have permission to modify failover maintenance settings.')
            return redirect('plugins:netbox_windows_dhcp:dhcpfailover_list')
        failover = get_object_or_404(DHCPFailover, pk=pk)
        enabled = request.POST.get('maintenance_mode') == '1'
        notes = request.POST.get('maintenance_notes', '')
        _apply_maintenance(failover, enabled, notes, request.user)
        action = 'enabled' if enabled else 'disabled'
        messages.success(request, f'Maintenance mode {action} for failover "{failover}".')
        return redirect('plugins:netbox_windows_dhcp:dhcpfailover_list')


class DHCPScopeMaintenanceView(LoginRequiredMixin, View):
    def get(self, request, pk):
        from .models import DHCPScope
        scope = get_object_or_404(DHCPScope, pk=pk)
        return render(request, 'netbox_windows_dhcp/dhcpmaintenance_toggle.html', {
            'object': scope,
            'object_type': 'Scope',
            'default_enabled': not scope.maintenance_mode,
            'return_url': scope.get_absolute_url(),
        })

    def post(self, request, pk):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpscope'):
            messages.error(request, 'You do not have permission to modify scope maintenance settings.')
            return redirect('plugins:netbox_windows_dhcp:dhcpscope_list')
        from .models import DHCPScope
        scope = get_object_or_404(DHCPScope, pk=pk)
        enabled = request.POST.get('maintenance_mode') == '1'
        notes = request.POST.get('maintenance_notes', '')
        _apply_maintenance(scope, enabled, notes, request.user)
        action = 'enabled' if enabled else 'disabled'
        messages.success(request, f'Maintenance mode {action} for scope "{scope}".')
        return redirect('plugins:netbox_windows_dhcp:dhcpscope_list')


# ---------------------------------------------------------------------------
# Maintenance mode — bulk toggle views
# ---------------------------------------------------------------------------

class DHCPServerBulkMaintenanceView(LoginRequiredMixin, View):
    def post(self, request):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpserver'):
            messages.error(request, 'You do not have permission to modify server maintenance settings.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        pk_list = [int(pk) for pk in request.POST.getlist('pk') if str(pk).isdigit()]
        if not pk_list:
            messages.warning(request, 'No servers selected.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        if request.POST.get('confirm'):
            enabled = request.POST.get('maintenance_mode') == '1'
            notes = request.POST.get('maintenance_notes', '')
            objs = DHCPServer.objects.filter(pk__in=pk_list)
            count = objs.count()
            for obj in objs:
                _apply_maintenance(obj, enabled, notes, request.user)
            action = 'enabled' if enabled else 'disabled'
            messages.success(request, f'Maintenance mode {action} for {count} server(s).')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        from django.urls import reverse
        return render(request, 'netbox_windows_dhcp/dhcpmaintenance_bulk.html', {
            'objects': DHCPServer.objects.filter(pk__in=pk_list),
            'object_type': 'Server',
            'pk_list': pk_list,
            'return_url': reverse('plugins:netbox_windows_dhcp:dhcpserver_list'),
        })


class DHCPFailoverBulkMaintenanceView(LoginRequiredMixin, View):
    def post(self, request):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpfailover'):
            messages.error(request, 'You do not have permission to modify failover maintenance settings.')
            return redirect('plugins:netbox_windows_dhcp:dhcpfailover_list')
        pk_list = [int(pk) for pk in request.POST.getlist('pk') if str(pk).isdigit()]
        if not pk_list:
            messages.warning(request, 'No failover relationships selected.')
            return redirect('plugins:netbox_windows_dhcp:dhcpfailover_list')
        if request.POST.get('confirm'):
            enabled = request.POST.get('maintenance_mode') == '1'
            notes = request.POST.get('maintenance_notes', '')
            objs = DHCPFailover.objects.filter(pk__in=pk_list)
            count = objs.count()
            for obj in objs:
                _apply_maintenance(obj, enabled, notes, request.user)
            action = 'enabled' if enabled else 'disabled'
            messages.success(request, f'Maintenance mode {action} for {count} failover relationship(s).')
            return redirect('plugins:netbox_windows_dhcp:dhcpfailover_list')
        from django.urls import reverse
        return render(request, 'netbox_windows_dhcp/dhcpmaintenance_bulk.html', {
            'objects': DHCPFailover.objects.filter(pk__in=pk_list),
            'object_type': 'Failover',
            'pk_list': pk_list,
            'return_url': reverse('plugins:netbox_windows_dhcp:dhcpfailover_list'),
        })


class DHCPScopeBulkMaintenanceView(LoginRequiredMixin, View):
    def post(self, request):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpscope'):
            messages.error(request, 'You do not have permission to modify scope maintenance settings.')
            return redirect('plugins:netbox_windows_dhcp:dhcpscope_list')
        pk_list = [int(pk) for pk in request.POST.getlist('pk') if str(pk).isdigit()]
        if not pk_list:
            messages.warning(request, 'No scopes selected.')
            return redirect('plugins:netbox_windows_dhcp:dhcpscope_list')
        if request.POST.get('confirm'):
            enabled = request.POST.get('maintenance_mode') == '1'
            notes = request.POST.get('maintenance_notes', '')
            from .models import DHCPScope
            objs = DHCPScope.objects.filter(pk__in=pk_list)
            count = objs.count()
            for obj in objs:
                _apply_maintenance(obj, enabled, notes, request.user)
            action = 'enabled' if enabled else 'disabled'
            messages.success(request, f'Maintenance mode {action} for {count} scope(s).')
            return redirect('plugins:netbox_windows_dhcp:dhcpscope_list')
        from django.urls import reverse
        from .models import DHCPScope
        return render(request, 'netbox_windows_dhcp/dhcpmaintenance_bulk.html', {
            'objects': DHCPScope.objects.filter(pk__in=pk_list),
            'object_type': 'Scope',
            'pk_list': pk_list,
            'return_url': reverse('plugins:netbox_windows_dhcp:dhcpscope_list'),
        })


# ---------------------------------------------------------------------------
# Current Maintenance combined view
# ---------------------------------------------------------------------------

class DHCPCurrentMaintenanceView(LoginRequiredMixin, View):
    template_name = 'netbox_windows_dhcp/dhcpcurrentmaintenance.html'

    def get(self, request):
        from .models import DHCPScope
        filter_type = request.GET.get('type', 'all')

        items = []

        if filter_type in ('all', 'server'):
            for server in DHCPServer.objects.filter(maintenance_mode=True).select_related(
                'maintenance_enabled_by'
            ):
                items.append({
                    'type': 'Server',
                    'type_class': 'badge-outline text-primary',
                    'object': server,
                    'url': server.get_absolute_url(),
                    'enabled_by': server.maintenance_enabled_by,
                    'enabled_at': server.maintenance_enabled_at,
                    'notes': server.maintenance_notes,
                    'health_status': server.health_status,
                    'health_class': {
                        'healthy': 'text-bg-success',
                        'unreachable': 'text-bg-danger',
                    }.get(server.health_status, 'text-bg-secondary'),
                })

        if filter_type in ('all', 'failover'):
            for fo in DHCPFailover.objects.filter(maintenance_mode=True).select_related(
                'maintenance_enabled_by', 'primary_server', 'secondary_server'
            ):
                items.append({
                    'type': 'Failover',
                    'type_class': 'badge-outline text-info',
                    'object': fo,
                    'url': fo.get_absolute_url(),
                    'enabled_by': fo.maintenance_enabled_by,
                    'enabled_at': fo.maintenance_enabled_at,
                    'notes': fo.maintenance_notes,
                    'health_status': None,
                    'health_class': None,
                })

        if filter_type in ('all', 'scope'):
            for scope in DHCPScope.objects.filter(maintenance_mode=True).select_related(
                'maintenance_enabled_by', 'prefix', 'server', 'failover'
            ):
                items.append({
                    'type': 'Scope',
                    'type_class': 'badge-outline text-secondary',
                    'object': scope,
                    'url': scope.get_absolute_url(),
                    'enabled_by': scope.maintenance_enabled_by,
                    'enabled_at': scope.maintenance_enabled_at,
                    'notes': scope.maintenance_notes,
                    'health_status': None,
                    'health_class': None,
                })

        return render(request, self.template_name, {
            'items': items,
            'filter_type': filter_type,
        })


# ---------------------------------------------------------------------------
# Settings view helpers
# ---------------------------------------------------------------------------

SYNC_JOB_NAME = 'Windows DHCP Sync'


def _get_next_sync_job():
    """Return the next scheduled or pending DHCPSyncJob Job row, or None."""
    from core.models import Job
    from django.db.models import F
    return (
        Job.objects.filter(name=SYNC_JOB_NAME, status__in=['scheduled', 'pending'])
        .order_by(F('scheduled').asc(nulls_last=True))
        .first()
    )


def _apply_interval_to_job(new_interval):
    """
    Update the interval (and reschedule datetime) on any existing scheduled/pending
    DHCPSyncJob. Does nothing if no job exists.
    """
    from core.models import Job
    from django.utils import timezone
    from datetime import timedelta
    from django.db.models import F

    job = (
        Job.objects.filter(name=SYNC_JOB_NAME, status__in=['scheduled', 'pending'])
        .order_by(F('scheduled').asc(nulls_last=True))
        .first()
    )
    if not job:
        return

    job.interval = new_interval
    update_fields = ['interval']
    if job.status == 'scheduled':
        job.scheduled = timezone.now() + timedelta(minutes=new_interval)
        update_fields.append('scheduled')
    job.save(update_fields=update_fields)


# ---------------------------------------------------------------------------
# Settings view
# ---------------------------------------------------------------------------

_SETTINGS_OVERRIDE_LABELS = {
    'sync_ips_from_dhcp': 'Sync IP Addresses from Leases & Reservations',
    'push_reservations': 'Push Reservations to DHCP Server',
    'push_scope_info': 'Push Scope Info to DHCP Server',
}


def _get_active_settings_overrides():
    """Return list of (label, value) tuples for any active PLUGINS_CONFIG boolean overrides."""
    from django.conf import settings as django_settings
    plugin_cfg = getattr(django_settings, 'PLUGINS_CONFIG', {}).get('netbox_windows_dhcp', {})
    result = []
    for cfg_key, label in _SETTINGS_OVERRIDE_LABELS.items():
        val = plugin_cfg.get(cfg_key)
        if val is not None:
            result.append((label, val))
    return result


class SettingsView(LoginRequiredMixin, View):
    """View/edit plugin-wide settings. Superuser only."""

    template_name = 'netbox_windows_dhcp/settings.html'

    def get(self, request):
        if not request.user.is_superuser:
            messages.error(request, 'Only superusers can manage plugin settings.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')

        from .models import DHCPPluginSettings
        form = PluginSettingsForm(instance=DHCPPluginSettings.load())
        return render(request, self.template_name, {
            'form': form,
            'next_sync_job': _get_next_sync_job(),
            'active_global_overrides': _get_active_settings_overrides(),
        })

    def post(self, request):
        if not request.user.is_superuser:
            messages.error(request, 'Only superusers can manage plugin settings.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')

        from .models import DHCPPluginSettings
        form = PluginSettingsForm(request.POST, instance=DHCPPluginSettings.load())
        if form.is_valid():
            form.save()
            _apply_interval_to_job(form.cleaned_data['sync_interval'])
            messages.success(request, 'Settings saved.')
            return redirect('plugins:netbox_windows_dhcp:settings')

        return render(request, self.template_name, {
            'form': form,
            'next_sync_job': _get_next_sync_job(),
            'active_global_overrides': _get_active_settings_overrides(),
        })


class ScheduleSyncView(LoginRequiredMixin, View):
    """
    Handle 'Run Now' and 'Schedule' actions for the recurring DHCPSyncJob.

    POST action=run_now  — cancel any existing scheduled job, enqueue immediately
                           (job auto-reschedules after it completes).
    POST action=schedule — cancel any existing scheduled job, create a new one
                           scheduled at the user-supplied start_at datetime.
    """

    def post(self, request):
        if not request.user.is_superuser:
            messages.error(request, 'Superuser access required.')
            return redirect('plugins:netbox_windows_dhcp:settings')

        from core.models import Job
        from django.utils import timezone

        from .background_tasks import DHCPSyncJob
        from .models import DHCPPluginSettings

        cfg = DHCPPluginSettings.load()
        action = request.POST.get('action', 'schedule')

        # Cancel any existing scheduled job (safe to delete — it hasn't entered the
        # RQ queue yet; pending jobs are already queued so we leave those alone).
        Job.objects.filter(name=SYNC_JOB_NAME, status='scheduled').delete()

        if action == 'run_now':
            job = DHCPSyncJob.enqueue(
                user=request.user,
                interval=cfg.sync_interval,
                queue_name=cfg.sync_queue,
            )
            messages.success(
                request,
                f'Sync enqueued — it will run shortly and reschedule every '
                f'{cfg.sync_interval} minute(s) after completion.',
            )
            return redirect(job.get_absolute_url())

        else:  # schedule
            from django.utils.dateparse import parse_datetime

            raw = request.POST.get('start_at', '').strip()
            scheduled_at = parse_datetime(raw)
            if not scheduled_at:
                messages.error(request, 'Please enter a valid start date/time.')
                return redirect('plugins:netbox_windows_dhcp:settings')

            # datetime-local inputs are naive (no tz); treat as server local time
            if timezone.is_naive(scheduled_at):
                scheduled_at = timezone.make_aware(scheduled_at)

            if scheduled_at <= timezone.now():
                messages.error(request, 'Start time must be in the future.')
                return redirect('plugins:netbox_windows_dhcp:settings')

            DHCPSyncJob.enqueue(
                user=request.user,
                schedule_at=scheduled_at,
                interval=cfg.sync_interval,
                queue_name=cfg.sync_queue,
            )
            messages.success(
                request,
                f'Sync scheduled to start at {scheduled_at.strftime("%b %-d, %Y %-I:%M %p")}, '
                f'then every {cfg.sync_interval} minute(s) thereafter.',
            )
            return redirect('plugins:netbox_windows_dhcp:settings')


# ---------------------------------------------------------------------------
# AJAX — Cert Fetch
# ---------------------------------------------------------------------------

class DHCPServerCertFetchView(LoginRequiredMixin, View):
    """AJAX POST: fetch TLS cert from a hostname. Returns JSON with cert details."""

    def post(self, request):
        from .cert_utils import fetch_cert_info

        hostname = request.POST.get('hostname', '').strip()
        port_str = request.POST.get('port', '443').strip()
        use_https = request.POST.get('use_https', 'true').lower() in ('1', 'true', 'on')

        if not hostname:
            return JsonResponse({'ok': False, 'message': 'Hostname is required'})
        if not use_https:
            return JsonResponse({'ok': False, 'message': 'Certificate fetch requires HTTPS to be enabled'})

        try:
            port = int(port_str)
        except (ValueError, TypeError):
            port = 443

        try:
            info = fetch_cert_info(hostname, port)
            not_after = info['not_after']
            return JsonResponse({
                'ok': True,
                'cert_info': {
                    'pem': info['pem'],
                    'fingerprint': info.get('fingerprint', ''),
                    'subject_cn': info.get('subject_cn', ''),
                    'sans': info.get('sans', []),
                    'issuer_cn': info.get('issuer_cn', ''),
                    'expiry_display': not_after.strftime('%B %-d, %Y') if not_after else '',
                    'expiry_iso': not_after.isoformat() if not_after else '',
                },
            })
        except OSError as exc:
            return JsonResponse({'ok': False, 'message': f'Could not connect to {hostname}:{port} — {exc}'})
        except Exception as exc:
            return JsonResponse({'ok': False, 'message': str(exc)})


# ---------------------------------------------------------------------------
# AJAX — Test Connection
# ---------------------------------------------------------------------------

class DHCPServerTestConnectionView(LoginRequiredMixin, View):
    """AJAX POST: test PSU connectivity with live form values. Returns JSON."""

    def post(self, request, pk=None):
        from .api_client import PSUClient, PSUClientError

        hostname = request.POST.get('hostname', '').strip()
        port_str = request.POST.get('port', '443').strip()
        use_https = request.POST.get('use_https', 'true').lower() in ('1', 'true', 'on')
        api_key = request.POST.get('api_key', '').strip()
        verify_ssl = request.POST.get('verify_ssl', 'true').lower() in ('1', 'true', 'on')
        ca_cert = request.POST.get('ca_cert', '').strip()

        if not hostname:
            return JsonResponse({'ok': False, 'message': 'Hostname is required'})

        if not api_key:
            if pk:
                try:
                    server = DHCPServer.objects.get(pk=pk)
                    api_key = server.api_key
                except DHCPServer.DoesNotExist:
                    pass
            if not api_key:
                return JsonResponse({'ok': False, 'message': 'API key required — enter an App Token in the field above'})

        try:
            port = int(port_str)
        except (ValueError, TypeError):
            port = 443

        # Build a transient server-like object
        class _TransientServer:
            pass

        server_obj = _TransientServer()
        server_obj.name = hostname
        server_obj.hostname = hostname
        server_obj.port = port
        server_obj.use_https = use_https
        server_obj.verify_ssl = verify_ssl
        server_obj.ca_cert = ca_cert
        server_obj.ca_cert_expiry = None
        server_obj.api_key = api_key

        scheme = 'https' if use_https else 'http'
        server_obj.base_url = f'{scheme}://{hostname}:{port}/api/dhcp'

        try:
            client = PSUClient(server_obj)
        except PSUClientError as exc:
            return JsonResponse({'ok': False, 'message': str(exc)})

        # Test read access
        try:
            client.ping_read()
        except PSUClientError as exc:
            msg = str(exc)
            sc = exc.status_code
            if sc == 401:
                msg = 'Authentication failed (401) — check API key'
            elif sc == 403:
                msg = 'Permission denied (403)'
            return JsonResponse({'ok': False, 'message': msg})
        except Exception as exc:
            return JsonResponse({'ok': False, 'message': str(exc)})

        # Test write access
        access = 'Read-Only'
        try:
            client.ping_write()
            access = 'Read-Write'
        except PSUClientError as exc:
            if exc.status_code == 403:
                access = 'Read-Only'
            else:
                return JsonResponse({'ok': False, 'message': str(exc)})
        except Exception:
            pass

        return JsonResponse({'ok': True, 'message': f'Connection: Good · Access: {access}'})


class DHCPServerPSUUpdateView(LoginRequiredMixin, View):
    """Enqueues a PSU script update job for a single DHCP server."""

    def post(self, request, pk):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpserver'):
            messages.error(request, 'You do not have permission to update PSU scripts.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        server = get_object_or_404(DHCPServer, pk=pk)
        from .background_tasks import DHCPPSUUpdateJob
        from .models import DHCPPluginSettings
        cfg = DHCPPluginSettings.load()
        job = DHCPPSUUpdateJob.enqueue(
            name=f'PSU Script Update: {server.name}',
            user=request.user,
            server_pk=server.pk,
            queue_name=cfg.sync_queue,
        )
        messages.success(request, f'PSU script update job queued for {server.name}.')
        return redirect(job.get_absolute_url())


class DHCPServerBulkPSUUpdateView(LoginRequiredMixin, View):
    """Enqueues PSU script update jobs for selected DHCP servers."""

    def post(self, request):
        if not request.user.has_perm('netbox_windows_dhcp.change_dhcpserver'):
            messages.error(request, 'You do not have permission to update PSU scripts.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        pk_list = [int(pk) for pk in request.POST.getlist('pk') if str(pk).isdigit()]
        if not pk_list:
            messages.warning(request, 'No servers selected.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
        from .background_tasks import DHCPPSUUpdateJob
        from .models import DHCPPluginSettings
        cfg = DHCPPluginSettings.load()
        count = 0
        for server in DHCPServer.objects.filter(pk__in=pk_list):
            DHCPPSUUpdateJob.enqueue(
                name=f'PSU Script Update: {server.name}',
                user=request.user,
                server_pk=server.pk,
                queue_name=cfg.sync_queue,
            )
            count += 1
        messages.success(request, f'PSU script update jobs queued for {count} server(s). Check System → Jobs for progress.')
        return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')
