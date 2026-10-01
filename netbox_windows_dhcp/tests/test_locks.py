"""
What users are refused (locks.py): the IP lock, the range and exclusion guard, the
Push Scope Info off locks in the UI and the REST API, option values in use, and
failovers (read-only except for their NetBox fields).
"""

from unittest import mock

from django.core.exceptions import ValidationError
from django.urls import reverse
from extras.models import Tag
from ipam.models import IPAddress, VRF
from utilities.exceptions import AbortRequest
from utilities.testing import APITestCase, TestCase

from ..models import DHCPExclusionRange, DHCPFailover, DHCPLeaseInfo, DHCPOptionValue, DHCPScope
from ..signals import validate_dhcp_ip_status
from ..utils import plugin_write
from .base import (
    api_url,
    error_messages,
    make_failover,
    make_option_value,
    make_prefix,
    make_scope,
    make_server,
    set_plugin_settings,
    ui_url,
)


PUSH_ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPScopePushJob.enqueue'
DELETE_ENQUEUE = 'netbox_windows_dhcp.background_tasks.DHCPScopeDeleteJob.enqueue'


def _ip(address, status='active', vrf=None):
    ip = IPAddress.objects.create(address=address, status=status, vrf=vrf)
    ip.refresh_from_db()
    return ip


def _check(ip):
    validate_dhcp_ip_status(sender=IPAddress, instance=ip)


class _LockFixture:
    """Scope 10.0.1.0/24, range .10–.200, exclusion .50–.59; sync IPs on, push off."""

    def setUp(self):
        super().setUp()
        self.protect = Tag.objects.create(name='DHCP Protect', slug='dhcp-protect')
        set_plugin_settings(
            sync_ip_addresses=True, push_reservations=False, push_scope_info=False,
            lease_status='dhcp', reservation_status='reserved', sync_protect_tag=self.protect,
        )
        self.prefix = make_prefix('10.0.1.0/24')
        self.scope = make_scope(prefix=self.prefix, start_ip='10.0.1.10', end_ip='10.0.1.200')
        self.exclusion = DHCPExclusionRange.objects.create(
            scope=self.scope, start_ip='10.0.1.50', end_ip='10.0.1.59',
        )


class _Fixture:
    """A scope with a prefix, one without, an exclusion, and a used and an unused option value; push off."""

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        set_plugin_settings(push_scope_info=False, push_reservations=False, sync_ip_addresses=False)
        self.tag = Tag.objects.create(name='Reviewed', slug='reviewed')
        self.server = make_server()
        self.scope = make_scope(name='Linked', server=self.server, start_ip='10.0.1.10', end_ip='10.0.1.200')
        self.unassigned = DHCPScope.objects.create(
            name='Unassigned', network='10.0.2.0', prefix_length=24, server=self.server,
            start_ip='10.0.2.10', end_ip='10.0.2.200',
        )
        self.exclusion = DHCPExclusionRange.objects.create(
            scope=self.scope, start_ip='10.0.1.50', end_ip='10.0.1.59', description='Printers',
        )
        self.used_option = make_option_value(value='10.0.0.53', friendly_name='DNS "main"')
        self.unused_option = make_option_value(
            option_definition=self.used_option.option_definition, value='10.0.0.54',
        )
        self.scope.option_values.add(self.used_option)
        self.unassigned.option_values.add(self.used_option)


class IPLockTests(_LockFixture, TestCase):

    def test_hand_made_ip_inside_range_is_refused(self):
        with self.assertRaisesMessage(ValidationError, 'Scope 1'):
            _check(IPAddress(address='10.0.1.20/24', status='active'))

    def test_reserved_ip_inside_range_is_refused_with_push_off(self):
        with self.assertRaises(ValidationError):
            _check(IPAddress(address='10.0.1.20/24', status='reserved'))

    def test_outside_range_and_inside_exclusion_are_allowed(self):
        _check(IPAddress(address='10.0.1.220/24', status='active'))
        _check(IPAddress(address='10.0.1.55/24', status='active'))

    def test_other_vrf_is_allowed(self):
        vrf = VRF.objects.create(name='Other')
        _check(IPAddress(address='10.0.1.20/24', status='active', vrf=vrf))

    def test_lock_is_off_when_sync_ip_addresses_is_off(self):
        set_plugin_settings(sync_ip_addresses=False)
        _check(IPAddress(address='10.0.1.20/24', status='active'))

    def test_pending_protect_tag_exempts(self):
        ip = IPAddress(address='10.0.1.20/24', status='active')
        ip._m2m_values = {'tags': [self.protect]}
        _check(ip)

    def test_saved_protect_tag_exempts(self):
        with plugin_write():  # setup
            ip = _ip('10.0.1.20/24')
        ip.tags.add(self.protect)
        ip.dns_name = 'edited'
        _check(ip)

    def test_untagged_ip_inside_protected_prefix_is_protected(self):
        self.prefix.tags.add(self.protect)
        _check(IPAddress(address='10.0.1.20/24', status='active'))

    def test_push_on_allows_reserved_only(self):
        set_plugin_settings(push_reservations=True)
        _check(IPAddress(address='10.0.1.20/24', status='reserved'))
        with self.assertRaises(ValidationError):
            _check(IPAddress(address='10.0.1.21/24', status='active'))

    def test_dhcp_managed_ip_can_be_edited_but_not_moved(self):
        ip = _ip('10.0.1.20/24', status='dhcp')
        DHCPLeaseInfo.objects.create(ip_address=ip, active=True)
        ip.status = 'reserved'
        ip.description = 'edited'
        _check(ip)

        ip.address = '10.0.1.21/24'
        with self.assertRaises(ValidationError):
            _check(ip)

    def test_hand_made_ip_with_dhcp_status_but_no_lease_info_is_refused(self):
        ip = _ip('10.0.1.220/24', status='dhcp')
        ip.address = '10.0.1.20/24'
        with self.assertRaises(ValidationError):
            _check(ip)

    def test_plugin_writes_bypass_the_lock(self):
        with plugin_write():
            _check(IPAddress(address='10.0.1.20/24', status='active'))

    def test_lease_status_needs_a_scope_even_with_sync_off(self):
        set_plugin_settings(sync_ip_addresses=False)
        red = VRF.objects.create(name='Red')
        refused = (
            ('10.0.1.55/24', None),   # inside an exclusion
            ('10.9.9.20/24', None),   # outside every scope
            ('10.0.1.20/24', red),    # a scope, but in another VRF
        )
        for address, vrf in refused:
            with self.subTest(address=address, vrf=vrf):
                with self.assertRaises(ValidationError):
                    _check(_ip(address, status='dhcp', vrf=vrf))
        _check(_ip('10.0.1.20/24', status='dhcp'))
        _check(_ip('10.9.9.20/24', status='active'))  # only the lease status is checked
        make_scope(name='Red', prefix=make_prefix('10.0.1.0/24', vrf=red),
                   server=make_server(name='Red Server', hostname='red.example.com'))
        _check(_ip('10.0.1.21/24', status='dhcp', vrf=red))

    def test_full_clean_runs_the_lock(self):
        with self.assertRaises(ValidationError):
            IPAddress(address='10.0.1.20/24', status='active').full_clean()


class RangeGuardTests(_LockFixture, TestCase):

    def setUp(self):
        super().setUp()
        # .220 sits outside the range; widening the range to .250 would cover it.
        self.outside = _ip('10.0.1.220/24')

    def _widen(self):
        self.scope.end_ip = '10.0.1.250'
        self.scope.full_clean()

    def test_widening_over_a_hand_made_ip_is_refused(self):
        with self.assertRaisesMessage(ValidationError, '10.0.1.220'):
            self._widen()

    def test_widening_over_allowed_ips_is_fine(self):
        self.outside.tags.add(self.protect)
        self._widen()

    def test_widening_over_a_reserved_ip_needs_push_on(self):
        IPAddress.objects.filter(pk=self.outside.pk).update(status='reserved')
        with self.assertRaises(ValidationError):
            self._widen()
        set_plugin_settings(push_reservations=True)
        self._widen()

    def test_widening_over_a_dhcp_managed_ip_is_fine(self):
        IPAddress.objects.filter(pk=self.outside.pk).update(status='dhcp')
        DHCPLeaseInfo.objects.create(ip_address=self.outside, active=True)
        self._widen()

    def test_unrelated_edit_ignores_ips_already_covered(self):
        with plugin_write():  # e.g. left over from before the upgrade
            _ip('10.0.1.30/24')
        self.scope.name = 'Renamed'
        self.scope.full_clean()

    def test_new_scope_over_a_hand_made_ip_is_refused(self):
        prefix = make_prefix('10.0.2.0/24')
        _ip('10.0.2.20/24')
        scope = DHCPScope(
            name='New', prefix=prefix, server=self.scope.server,
            start_ip='10.0.2.10', end_ip='10.0.2.200',
        )
        with self.assertRaises(ValidationError):
            scope.full_clean()

    def test_guard_is_off_when_sync_ip_addresses_is_off(self):
        set_plugin_settings(sync_ip_addresses=False)
        self._widen()

    def test_shrinking_an_exclusion_over_a_hand_made_ip_is_refused(self):
        _ip('10.0.1.55/24')
        self.exclusion.end_ip = '10.0.1.54'
        with self.assertRaisesMessage(ValidationError, '10.0.1.55'):
            self.exclusion.full_clean()

    def test_new_exclusion_is_never_refused(self):
        _ip('10.0.1.55/24')
        DHCPExclusionRange(scope=self.scope, start_ip='10.0.1.60', end_ip='10.0.1.70').full_clean()

    def test_deleting_an_exclusion_over_a_hand_made_ip_is_refused(self):
        _ip('10.0.1.55/24')
        with self.assertRaises(AbortRequest):
            self.exclusion.delete()
        self.assertTrue(DHCPExclusionRange.objects.filter(pk=self.exclusion.pk).exists())

    def test_deleting_an_empty_exclusion_is_fine(self):
        self.exclusion.delete()

    def test_deleting_the_scope_takes_its_exclusions_along(self):
        _ip('10.0.1.55/24')
        self.scope.delete()
        self.assertFalse(DHCPExclusionRange.objects.filter(pk=self.exclusion.pk).exists())

    def test_sync_writes_bypass_the_exclusion_delete_guard(self):
        _ip('10.0.1.55/24')
        with plugin_write():
            self.exclusion.delete()


class ScopeInfoUILockTests(_LockFixture, TestCase):
    """
    push_scope_info off: creating exclusions and option values, and editing option
    values, redirect; their buttons are hidden. Deletes and the exclusion edit page stay
    open (see the PushOff tests below).
    """

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        self.option_value = make_option_value()

    def test_write_views_are_refused(self):
        for url in (
            ui_url('dhcpexclusionrange_add'),
            ui_url('dhcpoptionvalue_add'),
            ui_url('dhcpoptionvalue_edit', self.option_value.pk),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertHttpStatus(response, 302)
                self.assertTrue(error_messages(response))

    def test_buttons_hidden(self):
        add_exclusion = ui_url('dhcpexclusionrange_add')
        self.assertNotContains(self.client.get(self.scope.get_absolute_url()), add_exclusion)
        self.assertNotContains(self.client.get(ui_url('dhcpexclusionrange_list')), add_exclusion)
        # The exclusion's own edit and delete buttons stay: both pages stay open.
        exclusion_page = self.client.get(self.exclusion.get_absolute_url())
        self.assertContains(exclusion_page, ui_url('dhcpexclusionrange_edit', self.exclusion.pk))
        self.assertContains(exclusion_page, ui_url('dhcpexclusionrange_delete', self.exclusion.pk))
        option_page = self.client.get(self.option_value.get_absolute_url())
        self.assertNotContains(option_page, ui_url('dhcpoptionvalue_edit', self.option_value.pk))
        self.assertContains(option_page, ui_url('dhcpoptionvalue_delete', self.option_value.pk))

    def test_views_and_buttons_open_with_push_scope_info_on(self):
        set_plugin_settings(push_scope_info=True)
        self.assertEqual(self.client.get(ui_url('dhcpexclusionrange_add')).status_code, 200)
        self.assertEqual(self.client.get(ui_url('dhcpoptionvalue_add')).status_code, 200)
        self.assertContains(self.client.get(self.scope.get_absolute_url()), ui_url('dhcpexclusionrange_add'))

    def test_exclusion_delete_guard_shows_a_message(self):
        set_plugin_settings(push_scope_info=True)
        _ip('10.0.1.55/24')
        response = self.client.post(ui_url('dhcpexclusionrange_delete', self.exclusion.pk), {'confirm': True})
        self.assertTrue(error_messages(response))
        self.assertTrue(DHCPExclusionRange.objects.filter(pk=self.exclusion.pk).exists())


class LocksAPITests(_LockFixture, APITestCase):

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        self.option_value = make_option_value()

    def test_scope_info_writes_refused_with_push_scope_info_off(self):
        # Option value edits are refused outright; scope and exclusion edits only when
        # they change a field the server owns (see PushOffAPITests).
        self.assertHttpStatus(self.client.get(api_url('dhcpoptionvalue', self.option_value.pk), **self.header), 200)
        response = self.client.patch(
            api_url('dhcpoptionvalue', self.option_value.pk), {'friendly_name': 'x'},
            format='json', **self.header,
        )
        self.assertHttpStatus(response, 403)
        self.assertIn('read-only while "Push Scope Info" is disabled', response.json()['detail'])
        response = self.client.patch(
            api_url('dhcpscope', self.scope.pk), {'description': 'x'}, format='json', **self.header,
        )
        self.assertHttpStatus(response, 400)
        for model, data in (
            ('dhcpexclusionrange', {'scope_id': self.scope.pk, 'start_ip': '10.0.1.60', 'end_ip': '10.0.1.61'}),
            ('dhcpoptionvalue', {'option_definition_id': self.option_value.option_definition_id, 'value': 'x'}),
            ('dhcpscope', {'name': 'New', 'network': '10.0.9.0', 'prefix_length': 24,
                           'start_ip': '10.0.9.10', 'end_ip': '10.0.9.20', 'server_id': self.scope.server_id}),
        ):
            with self.subTest(model=model):
                response = self.client.post(api_url(model), data, format='json', **self.header)
                self.assertHttpStatus(response, 403)

    def test_scope_info_writes_allowed_with_push_scope_info_on(self):
        set_plugin_settings(push_scope_info=True)
        response = self.client.patch(
            api_url('dhcpoptionvalue', self.option_value.pk), {'friendly_name': 'x'},
            format='json', **self.header,
        )
        self.assertHttpStatus(response, 200)

    def test_exclusion_delete_guard(self):
        set_plugin_settings(push_scope_info=True)
        _ip('10.0.1.55/24')
        response = self.client.delete(api_url('dhcpexclusionrange', self.exclusion.pk), **self.header)
        self.assertHttpStatus(response, 400)
        self.assertTrue(DHCPExclusionRange.objects.filter(pk=self.exclusion.pk).exists())

    def test_range_guard(self):
        set_plugin_settings(push_scope_info=True)
        _ip('10.0.1.220/24')
        response = self.client.patch(
            api_url('dhcpscope', self.scope.pk), {'end_ip': '10.0.1.250'}, format='json', **self.header,
        )
        self.assertHttpStatus(response, 400)

    def test_ip_lock(self):
        url = reverse('ipam-api:ipaddress-list')
        response = self.client.post(url, {'address': '10.0.1.20/24', 'status': 'active'},
                                    format='json', **self.header)
        self.assertHttpStatus(response, 400)
        self.assertIn('inside the DHCP range', str(response.json()))

        response = self.client.post(
            url, {'address': '10.0.1.21/24', 'status': 'active', 'tags': [{'slug': 'dhcp-protect'}]},
            format='json', **self.header,
        )
        self.assertHttpStatus(response, 201)


class PushOffDeleteViewTests(_Fixture, TestCase):

    def test_scope_delete_is_allowed_and_queues_nothing(self):
        with mock.patch(DELETE_ENQUEUE) as enq, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(ui_url('dhcpscope_delete', self.scope.pk), {'confirm': True})
        self.assertHttpStatus(response, 302)
        self.assertFalse(DHCPScope.objects.filter(pk=self.scope.pk).exists())
        self.assertFalse(DHCPExclusionRange.objects.filter(pk=self.exclusion.pk).exists())
        self.assertFalse(enq.called)

    def test_scope_bulk_delete_is_allowed(self):
        with mock.patch(DELETE_ENQUEUE) as enq, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(ui_url('dhcpscope_bulk_delete'), {
                'pk': [self.scope.pk, self.unassigned.pk], 'confirm': True, '_confirm': True,
            })
        self.assertHttpStatus(response, 302)
        self.assertFalse(DHCPScope.objects.exists())
        self.assertFalse(enq.called)

    def test_exclusion_delete_and_bulk_delete_are_allowed(self):
        other = DHCPExclusionRange.objects.create(scope=self.scope, start_ip='10.0.1.70', end_ip='10.0.1.79')
        with mock.patch(PUSH_ENQUEUE) as enq, self.captureOnCommitCallbacks(execute=True):
            self.client.post(ui_url('dhcpexclusionrange_delete', self.exclusion.pk), {'confirm': True})
            self.client.post(ui_url('dhcpexclusionrange_bulk_delete'), {
                'pk': [other.pk], 'confirm': True, '_confirm': True,
            })
        self.assertFalse(DHCPExclusionRange.objects.exists())
        self.assertFalse(enq.called)

    def test_unused_option_value_delete_is_allowed(self):
        response = self.client.post(ui_url('dhcpoptionvalue_delete', self.unused_option.pk), {'confirm': True})
        self.assertHttpStatus(response, 302)
        self.assertFalse(DHCPOptionValue.objects.filter(pk=self.unused_option.pk).exists())

    def test_create_is_still_refused(self):
        for name in ('dhcpscope_add', 'dhcpexclusionrange_add', 'dhcpoptionvalue_add'):
            with self.subTest(view=name):
                response = self.client.get(ui_url(name))
                self.assertHttpStatus(response, 302)
                self.assertTrue(error_messages(response))

    def test_scope_bulk_edit_is_still_refused(self):
        response = self.client.post(ui_url('dhcpscope_bulk_edit'), {
            'pk': [self.scope.pk], 'description': 'x', '_apply': True,
        })
        self.assertTrue(error_messages(response))
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.description, '')

    def test_list_pages_keep_their_delete_buttons(self):
        self.assertContains(self.client.get(ui_url('dhcpexclusionrange_list')), ui_url('dhcpexclusionrange_bulk_delete'))
        self.assertContains(self.client.get(ui_url('dhcpoptionvalue_list')), ui_url('dhcpoptionvalue_bulk_delete'))


class OptionValueDeleteGuardViewTests(_Fixture, TestCase):

    def _check_refused(self):
        response = self.client.post(ui_url('dhcpoptionvalue_delete', self.used_option.pk), {'confirm': True})
        message = ' '.join(error_messages(response))
        for fact in ('2', 'Linked', 'Unassigned'):
            self.assertIn(fact, message)
        response = self.client.post(ui_url('dhcpoptionvalue_bulk_delete'), {
            'pk': [self.used_option.pk, self.unused_option.pk], 'confirm': True, '_confirm': True,
        })
        self.assertTrue(error_messages(response))
        # All or nothing: the unused one isn't deleted either.
        self.assertEqual(DHCPOptionValue.objects.count(), 2)

    def test_refused_with_push_off(self):
        self._check_refused()

    def test_refused_with_push_on(self):
        set_plugin_settings(push_scope_info=True)
        self._check_refused()

    def test_friendly_name_is_escaped(self):
        response = self.client.post(
            ui_url('dhcpoptionvalue_delete', self.used_option.pk), {'confirm': True}, follow=True,
        )
        self.assertContains(response, 'DNS &quot;main&quot;')

    def test_deletable_once_no_scope_uses_it(self):
        self.scope.option_values.remove(self.used_option)
        self.unassigned.option_values.remove(self.used_option)
        self.client.post(ui_url('dhcpoptionvalue_delete', self.used_option.pk), {'confirm': True})
        self.assertFalse(DHCPOptionValue.objects.filter(pk=self.used_option.pk).exists())

    def test_the_message_names_a_few_scopes(self):
        for i in range(3, 7):
            scope = DHCPScope.objects.create(
                name=f'Extra {i}', network=f'10.0.{i}.0', prefix_length=24, server=self.server,
                start_ip=f'10.0.{i}.10', end_ip=f'10.0.{i}.20',
            )
            scope.option_values.add(self.used_option)
        response = self.client.post(ui_url('dhcpoptionvalue_delete', self.used_option.pk), {'confirm': True})
        message = ' '.join(error_messages(response))
        self.assertIn('6', message)  # how many scopes use it
        names = ['Linked', 'Unassigned'] + [f'Extra {i}' for i in range(3, 7)]
        self.assertEqual(sum(name in message for name in names), 3)  # only a few are named


class PushOffScopeEditViewTests(_Fixture, TestCase):

    def test_edit_page_opens_with_server_fields_locked(self):
        response = self.client.get(ui_url('dhcpscope_edit', self.scope.pk))
        self.assertHttpStatus(response, 200)
        self.assertTrue(response.context['form'].fields['name'].disabled)

    def test_nothing_locked_with_push_on(self):
        set_plugin_settings(push_scope_info=True)
        response = self.client.get(ui_url('dhcpscope_edit', self.scope.pk))
        self.assertHttpStatus(response, 200)
        self.assertFalse(response.context['form'].fields['name'].disabled)

    def test_server_owned_fields_are_read_only(self):
        form = self.client.get(ui_url('dhcpscope_edit', self.scope.pk)).context['form']
        for name in ('name', 'active', 'description', 'start_ip', 'end_ip', 'router', 'lease_lifetime_value',
                     'lease_lifetime_unit', 'server', 'failover', 'option_values', 'network', 'prefix_length'):
            self.assertTrue(form.fields[name].disabled, name)
        for name in ('prefix', 'tags'):
            self.assertFalse(form.fields[name].disabled, name)

    def test_posted_server_fields_are_ignored_and_tags_save(self):
        with mock.patch(PUSH_ENQUEUE) as enq, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(ui_url('dhcpscope_edit', self.scope.pk), {
                'prefix': self.scope.prefix_id, 'tags': [self.tag.pk],
                'name': 'Changed', 'start_ip': '10.0.1.99', 'end_ip': '10.0.1.100',
                'lease_lifetime_value': 5, 'lease_lifetime_unit': 'minutes', 'option_values': [],
            })
        self.assertHttpStatus(response, 302)
        self.scope.refresh_from_db()
        self.assertEqual((self.scope.name, self.scope.start_ip, self.scope.end_ip),
                         ('Linked', '10.0.1.10', '10.0.1.200'))
        self.assertEqual(self.scope.lease_lifetime, 86400)
        self.assertEqual(list(self.scope.option_values.all()), [self.used_option])
        self.assertEqual(list(self.scope.tags.all()), [self.tag])
        self.assertFalse(enq.called)

    def test_linking_a_prefix_to_an_unassigned_scope(self):
        prefix = make_prefix('10.0.2.0/24')
        form = self.client.get(ui_url('dhcpscope_edit', self.unassigned.pk)).context['form']
        self.assertTrue(form.fields['network'].disabled)
        self.assertTrue(form.fields['prefix_length'].disabled)
        response = self.client.post(ui_url('dhcpscope_edit', self.unassigned.pk), {
            'prefix': prefix.pk, 'network': '10.0.9.0', 'prefix_length': 16,
        })
        self.assertHttpStatus(response, 302)
        self.unassigned.refresh_from_db()
        self.assertEqual(self.unassigned.prefix, prefix)
        self.assertEqual((self.unassigned.network, self.unassigned.prefix_length), ('10.0.2.0', 24))
        self.assertEqual(list(self.unassigned.option_values.all()), [self.used_option])

    def test_a_prefix_that_doesnt_match_is_refused(self):
        prefix = make_prefix('10.0.3.0/24')
        response = self.client.post(ui_url('dhcpscope_edit', self.unassigned.pk), {'prefix': prefix.pk})
        self.assertHttpStatus(response, 200)
        self.assertTrue(response.context['form'].errors)
        self.unassigned.refresh_from_db()
        self.assertIsNone(self.unassigned.prefix)


class PushOffExclusionEditViewTests(_Fixture, TestCase):

    def test_edit_page_opens_and_only_netbox_fields_change(self):
        response = self.client.get(ui_url('dhcpexclusionrange_edit', self.exclusion.pk))
        form = response.context['form']
        for name in ('scope', 'start_ip', 'end_ip'):
            self.assertTrue(form.fields[name].disabled, name)
        self.assertFalse(form.fields['description'].disabled)

        with mock.patch(PUSH_ENQUEUE) as enq, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(ui_url('dhcpexclusionrange_edit', self.exclusion.pk), {
                'description': 'Cameras', 'tags': [self.tag.pk],
                'start_ip': '10.0.1.60', 'end_ip': '10.0.1.61', 'scope': self.unassigned.pk,
            })
        self.assertHttpStatus(response, 302)
        self.exclusion.refresh_from_db()
        self.assertEqual(
            (self.exclusion.scope_id, self.exclusion.start_ip, self.exclusion.end_ip, self.exclusion.description),
            (self.scope.pk, '10.0.1.50', '10.0.1.59', 'Cameras'),
        )
        self.assertEqual(list(self.exclusion.tags.all()), [self.tag])
        self.assertFalse(enq.called)

    def test_option_value_edit_stays_refused(self):
        response = self.client.get(ui_url('dhcpoptionvalue_edit', self.unused_option.pk))
        self.assertHttpStatus(response, 302)
        self.assertTrue(error_messages(response))


class PushOffAPITests(_Fixture, APITestCase):

    def _patch(self, model, pk, data):
        return self.client.patch(api_url(model, pk), data, format='json', **self.header)

    def test_scope_prefix_and_tags_can_change(self):
        prefix = make_prefix('10.0.2.0/24')
        response = self._patch('dhcpscope', self.unassigned.pk, {'prefix_id': prefix.pk, 'tags': [self.tag.pk]})
        self.assertHttpStatus(response, 200)
        self.unassigned.refresh_from_db()
        self.assertEqual(self.unassigned.prefix, prefix)
        self.assertEqual(list(self.unassigned.tags.all()), [self.tag])

    def test_scope_server_fields_are_refused(self):
        other = make_server(name='Other', hostname='other.example.com')
        for data in (
            {'name': 'Changed'}, {'active': False}, {'end_ip': '10.0.1.150'}, {'lease_lifetime': 60},
            {'server_id': other.pk}, {'option_value_ids': []}, {'network': '10.0.9.0'},
        ):
            with self.subTest(data=data):
                response = self._patch('dhcpscope', self.scope.pk, data)
                self.assertHttpStatus(response, 400)
                self.assertIn('read-only while "Push Scope Info" is disabled', str(response.json()))
        self.scope.refresh_from_db()
        self.assertEqual(self.scope.name, 'Linked')

    def test_scope_values_sent_unchanged_are_fine(self):
        response = self._patch('dhcpscope', self.scope.pk, {
            'name': 'Linked', 'start_ip': '10.0.1.10', 'server_id': self.server.pk,
            'option_value_ids': [self.used_option.pk], 'lease_lifetime': self.scope.lease_lifetime,
            'network': '10.0.1.0', 'prefix_length': 24, 'description': '',
        })
        self.assertHttpStatus(response, 200)

    def test_an_unassigned_scopes_network_is_read_only_too(self):
        for data in ({'network': '10.0.8.0'}, {'prefix_length': 16}):
            with self.subTest(data=data):
                response = self._patch('dhcpscope', self.unassigned.pk, data)
                self.assertHttpStatus(response, 400)
                self.assertIn('read-only while "Push Scope Info" is disabled', str(response.json()))
        self.unassigned.refresh_from_db()
        self.assertEqual((self.unassigned.network, self.unassigned.prefix_length), ('10.0.2.0', 24))

    def test_exclusion_rules(self):
        response = self._patch('dhcpexclusionrange', self.exclusion.pk, {'description': 'Cameras'})
        self.assertHttpStatus(response, 200)
        response = self._patch('dhcpexclusionrange', self.exclusion.pk, {'end_ip': '10.0.1.58'})
        self.assertHttpStatus(response, 400)
        self.assertIn('end_ip', response.json())

    def test_option_value_edit_is_refused(self):
        response = self._patch('dhcpoptionvalue', self.unused_option.pk, {'friendly_name': 'x'})
        self.assertHttpStatus(response, 403)

    def test_deletes_are_allowed(self):
        with mock.patch(DELETE_ENQUEUE) as enq, self.captureOnCommitCallbacks(execute=True):
            for model, pk in (
                ('dhcpexclusionrange', self.exclusion.pk),
                ('dhcpoptionvalue', self.unused_option.pk),
                ('dhcpscope', self.scope.pk),
            ):
                with self.subTest(model=model):
                    self.assertHttpStatus(self.client.delete(api_url(model, pk), **self.header), 204)
        self.assertFalse(enq.called)

    def test_bulk_delete_is_allowed(self):
        response = self.client.delete(
            api_url('dhcpscope'), [{'id': self.scope.pk}, {'id': self.unassigned.pk}],
            format='json', **self.header,
        )
        self.assertHttpStatus(response, 204)
        self.assertFalse(DHCPScope.objects.exists())

    def test_option_value_delete_guard(self):
        for push in (False, True):
            set_plugin_settings(push_scope_info=push)
            with self.subTest(push_scope_info=push):
                response = self.client.delete(api_url('dhcpoptionvalue', self.used_option.pk), **self.header)
                self.assertHttpStatus(response, 400)
                self.assertIn('is used by 2 scope(s)', response.json()['detail'])
                response = self.client.delete(
                    api_url('dhcpoptionvalue'), [{'id': self.used_option.pk}], format='json', **self.header,
                )
                self.assertHttpStatus(response, 400)
        self.assertTrue(DHCPOptionValue.objects.filter(pk=self.used_option.pk).exists())


class FailoverEditPageTests(TestCase):

    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save()
        self.failover = make_failover(enable_auth=True, shared_secret='top-secret')

    def test_edit_page_hides_the_shared_secret(self):
        response = self.client.get(reverse('plugins:netbox_windows_dhcp:dhcpfailover_edit',
                                           kwargs={'pk': self.failover.pk}))
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, 'shared_secret')
        self.assertNotContains(response, 'top-secret')
        self.assertNotContains(response, 'name="sync_enabled"')
        self.assertContains(response, 'default_scope_vrf')

    def test_only_the_netbox_fields_are_saved(self):
        red = VRF.objects.create(name='Red')
        url = reverse('plugins:netbox_windows_dhcp:dhcpfailover_edit', kwargs={'pk': self.failover.pk})
        response = self.client.post(url, {
            'name': 'Changed', 'mode': 'HotStandby', 'max_response_delay': 99,
            'description': 'Lab pair', 'default_scope_vrf': red.pk,
        })
        self.assertHttpStatus(response, 302)
        self.failover.refresh_from_db()
        self.assertEqual((self.failover.description, self.failover.default_scope_vrf), ('Lab pair', red))
        self.assertEqual((self.failover.name, self.failover.mode, self.failover.max_response_delay),
                         ('Failover 1', 'LoadBalance', 30))
        self.assertEqual(self.failover.shared_secret, 'top-secret')

    def test_detail_page_shows_the_edit_button(self):
        response = self.client.get(self.failover.get_absolute_url())
        self.assertContains(response, reverse('plugins:netbox_windows_dhcp:dhcpfailover_edit',
                                              kwargs={'pk': self.failover.pk}))


class FailoverAPILockTests(APITestCase):

    def setUp(self):
        super().setUp()
        self.add_permissions(
            'netbox_windows_dhcp.view_dhcpfailover', 'netbox_windows_dhcp.add_dhcpfailover',
            'netbox_windows_dhcp.change_dhcpfailover', 'ipam.view_vrf',
        )
        self.primary = make_server(name='P', hostname='p')
        self.secondary = make_server(name='S', hostname='s')
        self.failover = make_failover(primary=self.primary, secondary=self.secondary)
        self.url = reverse('plugins-api:netbox_windows_dhcp-api:dhcpfailover-detail',
                           kwargs={'pk': self.failover.pk})

    def test_create_is_refused(self):
        response = self.client.post(
            reverse('plugins-api:netbox_windows_dhcp-api:dhcpfailover-list'),
            {'name': 'New', 'primary_server_id': self.primary.pk, 'secondary_server_id': self.secondary.pk},
            format='json', **self.header,
        )
        self.assertHttpStatus(response, 403)
        self.assertEqual(DHCPFailover.objects.count(), 1)

    def test_locked_fields_are_refused(self):
        for data in ({'name': 'Renamed'}, {'max_response_delay': 99},
                     {'secondary_server_id': make_server(name='T', hostname='t').pk}):
            with self.subTest(data=data):
                response = self.client.patch(self.url, data, format='json', **self.header)
                self.assertHttpStatus(response, 400)
        self.failover.refresh_from_db()
        self.assertEqual((self.failover.name, self.failover.max_response_delay, self.failover.sync_enabled),
                         ('Failover 1', 30, True))

    def test_netbox_fields_can_change(self):
        red = VRF.objects.create(name='Red')
        response = self.client.patch(
            self.url, {'default_scope_vrf_id': red.pk, 'description': 'Lab pair', 'tags': []},
            format='json', **self.header,
        )
        self.assertHttpStatus(response, 200)
        self.failover.refresh_from_db()
        self.assertEqual((self.failover.default_scope_vrf, self.failover.description), (red, 'Lab pair'))
        self.assertEqual(response.json()['default_scope_vrf']['id'], red.pk)

    def test_unchanged_locked_fields_are_accepted(self):
        response = self.client.patch(self.url, {'name': 'Failover 1', 'description': 'x'},
                                     format='json', **self.header)
        self.assertHttpStatus(response, 200)

    def test_shared_secret_is_never_exposed(self):
        response = self.client.get(self.url, **self.header)
        self.assertNotIn('shared_secret', response.json())
