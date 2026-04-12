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
        })

    def post(self, request):
        if not request.user.is_superuser:
            messages.error(request, 'Only superusers can manage plugin settings.')
            return redirect('plugins:netbox_windows_dhcp:dhcpserver_list')

        from .models import DHCPPluginSettings
        form = PluginSettingsForm(request.POST, instance=DHCPPluginSettings.load())
        if form.is_valid():
            form.save()
            messages.success(request, 'Settings saved.')
            return redirect('plugins:netbox_windows_dhcp:settings')

        return render(request, self.template_name, {
            'form': form,
            'missing_statuses': self._check_custom_statuses(),
        })
