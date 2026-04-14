import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
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


# ---------------------------------------------------------------------------
# DHCPServer views
# ---------------------------------------------------------------------------

class DHCPServerListView(generic.ObjectListView):
    queryset = DHCPServer.objects.all()
    table = DHCPServerTable
    filterset = DHCPServerFilterSet
    filterset_form = DHCPServerFilterForm


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

        return {
            'failover_table': failover_table,
            'scope_table': scope_table,
        }


class DHCPServerCreateView(generic.ObjectEditView):
    queryset = DHCPServer.objects.all()
    form = DHCPServerForm


class DHCPServerEditView(generic.ObjectEditView):
    queryset = DHCPServer.objects.all()
    form = DHCPServerForm


class DHCPServerDeleteView(generic.ObjectDeleteView):
    queryset = DHCPServer.objects.all()


class DHCPServerBulkDeleteView(generic.BulkDeleteView):
    queryset = DHCPServer.objects.all()
    table = DHCPServerTable


class DHCPServerSyncView(LoginRequiredMixin, View):
    """Enqueues a background sync job for a single DHCP server."""

    def post(self, request, pk):
        server = get_object_or_404(DHCPServer, pk=pk)
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

    def get(self, request, pk):
        return self.post(request, pk)


class DHCPGlobalSyncView(LoginRequiredMixin, View):
    """Enqueues a background sync job for every configured DHCP server."""

    def post(self, request):
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

    def get(self, request):
        return self.post(request)


class DHCPServerImportView(LoginRequiredMixin, View):
    """Enqueues a background import job for a DHCP server."""

    template_name = 'netbox_windows_dhcp/dhcpserver_import.html'

    def get(self, request, pk):
        server = get_object_or_404(DHCPServer, pk=pk)
        return render(request, self.template_name, {'object': server})

    def post(self, request, pk):
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
        failover = get_object_or_404(DHCPFailover, pk=pk)
        failover.sync_enabled = not failover.sync_enabled
        failover.save(update_fields=['sync_enabled'])
        state = 'enabled' if failover.sync_enabled else 'disabled'
        messages.success(request, f'Sync {state} for failover "{failover}".')
        return redirect(request.META.get('HTTP_REFERER', 'plugins:netbox_windows_dhcp:dhcpfailover_list'))


class DHCPFailoverBulkToggleSyncView(LoginRequiredMixin, View):
    """Toggle sync_enabled on all selected failover relationships."""

    def post(self, request):
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
        ip_qs = IPAddress.objects.annotate(
            _in_dynamic_range=RawSQL(
                "host(address)::inet >= %s::inet AND host(address)::inet <= %s::inet",
                (instance.start_ip, instance.end_ip),
                output_field=BooleanField(),
            )
        ).filter(
            Q(_in_dynamic_range=True)
            | Q(address__net_contained_or_equal=prefix_cidr, status='dhcp')
            | Q(address__net_contained_or_equal=prefix_cidr, status='reserved',
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
