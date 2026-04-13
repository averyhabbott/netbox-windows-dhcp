import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from netbox.views import generic

from .filtersets import (
    DHCPFailoverFilterSet,
    DHCPOptionCodeDefinitionFilterSet,
    DHCPOptionValueFilterSet,
    DHCPScopeFilterSet,
    DHCPServerFilterSet,
)
from .forms import (
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
    DHCPFailover,
    DHCPOptionCodeDefinition,
    DHCPOptionValue,
    DHCPScope,
    DHCPServer,
)
from .tables import (
    DHCPFailoverTable,
    DHCPOptionCodeDefinitionTable,
    DHCPOptionValueTable,
    DHCPScopeTable,
    DHCPServerTable,
)

logger = logging.getLogger('netbox_windows_dhcp')


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

        related_scopes = DHCPScope.objects.filter(failover__in=related_failovers)
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
        job = DHCPServerSyncJob.enqueue(
            name=f'Sync {server.name}',
            user=request.user,
            server_pk=server.pk,
        )
        messages.success(request, f'Sync job queued for {server.name}.')
        return redirect(job.get_absolute_url())

    def get(self, request, pk):
        return self.post(request, pk)


class DHCPGlobalSyncView(LoginRequiredMixin, View):
    """Enqueues a background sync job for every configured DHCP server."""

    def post(self, request):
        from .background_tasks import DHCPServerSyncJob
        servers = DHCPServer.objects.all()
        count = 0
        for server in servers:
            DHCPServerSyncJob.enqueue(
                name=f'Sync {server.name}',
                user=request.user,
                server_pk=server.pk,
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
        job = DHCPImportJob.enqueue(
            name=f'Import from {server.name}',
            user=request.user,
            server_pk=server.pk,
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


class DHCPFailoverView(generic.ObjectView):
    queryset = DHCPFailover.objects.select_related('primary_server', 'secondary_server')

    def get_extra_context(self, request, instance):
        scope_table = DHCPScopeTable(instance.scopes.all())
        scope_table.configure(request)
        return {'scope_table': scope_table}


class DHCPFailoverCreateView(generic.ObjectEditView):
    queryset = DHCPFailover.objects.all()
    form = DHCPFailoverForm


class DHCPFailoverEditView(generic.ObjectEditView):
    queryset = DHCPFailover.objects.all()
    form = DHCPFailoverForm


class DHCPFailoverDeleteView(generic.ObjectDeleteView):
    queryset = DHCPFailover.objects.all()


class DHCPFailoverBulkDeleteView(generic.BulkDeleteView):
    queryset = DHCPFailover.objects.all()
    table = DHCPFailoverTable


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
    queryset = DHCPScope.objects.select_related('prefix', 'failover')
    table = DHCPScopeTable
    filterset = DHCPScopeFilterSet
    filterset_form = DHCPScopeFilterForm


class DHCPScopeView(generic.ObjectView):
    queryset = DHCPScope.objects.select_related('prefix', 'failover').prefetch_related(
        'option_values__option_definition'
    )

    def get_extra_context(self, request, instance):
        option_table = DHCPOptionValueTable(instance.option_values.all())
        option_table.configure(request)
        return {'option_table': option_table}


class DHCPScopeCreateView(generic.ObjectEditView):
    queryset = DHCPScope.objects.all()
    form = DHCPScopeForm


class DHCPScopeEditView(generic.ObjectEditView):
    queryset = DHCPScope.objects.all()
    form = DHCPScopeForm


class DHCPScopeDeleteView(generic.ObjectDeleteView):
    queryset = DHCPScope.objects.all()


class DHCPScopeBulkEditView(generic.BulkEditView):
    queryset = DHCPScope.objects.select_related('prefix', 'failover')
    filterset = DHCPScopeFilterSet
    table = DHCPScopeTable
    form = DHCPScopeBulkEditForm

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

    def _check_custom_statuses(self):
        from django.apps import apps
        try:
            IPAddress = apps.get_model('ipam', 'IPAddress')
            choices = [c[0] for c in IPAddress._meta.get_field('status').choices]
            return [s for s in ('dhcp-lease', 'dhcp-reserved') if s not in choices]
        except Exception:
            return []

    def get(self, request):
        if not request.user.is_superuser:
            messages.error(request, 'Only superusers can manage plugin settings.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')

        from .models import DHCPPluginSettings
        form = PluginSettingsForm(instance=DHCPPluginSettings.load())
        return render(request, self.template_name, {
            'form': form,
            'missing_statuses': self._check_custom_statuses(),
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
            'missing_statuses': self._check_custom_statuses(),
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
            )
            messages.success(
                request,
                f'Sync scheduled to start at {scheduled_at.strftime("%b %-d, %Y %-I:%M %p")}, '
                f'then every {cfg.sync_interval} minute(s) thereafter.',
            )
            return redirect('plugins:netbox_windows_dhcp:settings')
