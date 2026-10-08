"""The bundled PSU script parses into the endpoint set the plugin deploys."""

from django.test import SimpleTestCase

from ..background_tasks import _parse_psu_script


class PSUScriptParseTests(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.endpoints = _parse_psu_script()
        cls.keys = {(e['method'], e['url']) for e in cls.endpoints}

    def test_bulk_reservation_endpoints_present(self):
        for method in ('GET', 'POST', 'PUT', 'DELETE'):
            self.assertIn((method, '/api/dhcp/reservations'), self.keys)

    def test_no_duplicate_endpoints(self):
        self.assertEqual(len(self.keys), len(self.endpoints))

    def test_every_endpoint_logs_under_its_own_name(self):
        # Platform > Logging tells endpoints apart by this name, so a copy-paste slip
        # would put one endpoint's log lines under another's.
        for e in self.endpoints:
            with self.subTest(method=e['method'], url=e['url']):
                self.assertIn(f"$LogResource = '{e['url']}:{e['method']}'", e['script_block'])

    def test_reservation_create_activates_an_active_lease_without_removing_it(self):
        # Add-DhcpServerv4Reservation alone leaves the lease InactiveReservation. The create
        # reads the lease first, then marks it ActiveReservation, and never removes a lease record.
        post = next(e for e in self.endpoints if (e['method'], e['url']) == ('POST', '/api/dhcp/reservations'))
        # The block starts with the shared helpers; the endpoint's own code follows its $LogResource.
        block = post['script_block'].split("$LogResource = '/api/dhcp/reservations:POST'")[1]
        self.assertEqual(block.count('Get-LeaseAt'), 2)  # single create and batch create
        self.assertEqual(block.count('Set-LeaseActiveReservation'), 2)
        self.assertLess(block.index('Get-LeaseAt'), block.index('Add-DhcpServerv4Reservation'))
        self.assertLess(block.index('Add-DhcpServerv4Reservation'), block.index('Set-LeaseActiveReservation'))
        whole = post['script_block']
        self.assertIn("AddressState = 'ActiveReservation'", whole)
        # -ScopeId and -IPAddress can't be combined on Get-DhcpServerv4Lease (ambiguous parameter set).
        self.assertIn('Get-DhcpServerv4Lease -IPAddress $IPAddress', whole)
        self.assertNotIn('Get-DhcpServerv4Lease -ScopeId $ScopeId -IPAddress', whole)
        self.assertNotIn('Remove-DhcpServerv4Lease', whole)
        self.assertNotIn("LeaseExpiryTime =", whole)
        self.assertNotIn("['LeaseExpiryTime']", whole)
