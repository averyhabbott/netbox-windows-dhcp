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
