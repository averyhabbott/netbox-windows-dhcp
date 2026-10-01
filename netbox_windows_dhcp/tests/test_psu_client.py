"""
Talking to PowerShell Universal: the requests PSUClient sends, how it reads the replies,
and reading a server's HTTPS certificate. Offline: the HTTP session and the socket are
mocks, so no connection is ever made.
"""

import datetime
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.test import SimpleTestCase, override_settings

from ..api_client import PSUClient, PSUClientError
from ..cert_utils import fetch_cert_info
from ..models import DHCPServer

BASE = 'https://dhcp.example.com:443'


def fake_response(status_code=200, json_data=None, content=b'{}'):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.content = content
    resp.text = content.decode() if isinstance(content, bytes) else str(content)
    resp.json.return_value = {} if json_data is None else json_data
    return resp


def build_client(**server_kwargs):
    """Build a PSUClient against an unsaved DHCPServer with a mocked session."""
    defaults = dict(name='S1', hostname='dhcp.example.com', port=443, use_https=True)
    defaults.update(server_kwargs)
    client = PSUClient(DHCPServer(**defaults))
    client.session = mock.Mock()
    return client


class RequestTests(SimpleTestCase):

    def test_each_call_reaches_its_endpoint(self):
        body = {'scope_id': '10.0.1.0', 'start_ip': '10.0.1.5', 'end_ip': '10.0.1.9'}
        cases = (
            ('list_scopes', (), fake_response(json_data=[]), 'GET', '/api/dhcp/scopes', None),
            ('get_scope', ('10.0.1.0',), fake_response(json_data={'scope_id': '10.0.1.0'}),
             'GET', '/api/dhcp/scopes/10.0.1.0', None),
            ('create_reservation', (body,), fake_response(json_data={}), 'POST', '/api/dhcp/reservations', body),
            ('delete_exclusion', (body,), fake_response(status_code=204, content=b''),
             'DELETE', '/api/dhcp/exclusions', body),
            ('get_dhcp_endpoints', (), fake_response(json_data=[]), 'GET', '/api/v1/endpoint', None),
        )
        for name, args, response, method, path, sent in cases:
            with self.subTest(name):
                client = build_client()
                client.session.request.return_value = response
                getattr(client, name)(*args)
                call_args, call_kwargs = client.session.request.call_args
                self.assertEqual(tuple(call_args[:2]), (method, BASE + path))
                if sent is not None:
                    self.assertEqual(call_kwargs['json'], sent)

    def test_scope_list_can_leave_out_the_router(self):
        client = build_client()
        client.session.request.return_value = fake_response(json_data=[])
        client.list_scopes()
        self.assertIsNone(client.session.request.call_args[1].get('params'))
        client.list_scopes(include_router=False)
        self.assertEqual(client.session.request.call_args[1]['params'], {'include_router': 'false'})

    def test_token_is_sent_and_a_config_override_wins(self):
        server = DHCPServer(name='S1', hostname='dhcp.example.com', api_key='model-token')
        self.assertEqual(PSUClient(server).session.headers['Authorization'], 'Bearer model-token')
        override = {'netbox_windows_dhcp': {'server_overrides': {'dhcp.example.com': {'api_key': 'override-token'}}}}
        with override_settings(PLUGINS_CONFIG=override):
            self.assertEqual(PSUClient(server).session.headers['Authorization'], 'Bearer override-token')

    def test_verify_ssl_setting_is_applied(self):
        for verify in (True, False):
            with self.subTest(verify_ssl=verify):
                server = DHCPServer(name='S', hostname='h.example.com', verify_ssl=verify, ca_cert='')
                self.assertEqual(PSUClient(server).session.verify, verify)


class ResponseTests(SimpleTestCase):

    def test_error_raises_with_status_code(self):
        client = build_client()
        client.session.request.return_value = fake_response(status_code=403, content=b'forbidden')
        with self.assertRaises(PSUClientError) as ctx:
            client.ping_write()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_lists_always_come_back_as_lists(self):
        # PowerShell sends a single item as a bare object and nothing as an empty reply.
        client = build_client()
        client.session.request.return_value = fake_response(json_data={'scope_id': '10.0.1.0'})
        self.assertEqual(client.list_scopes(), [{'scope_id': '10.0.1.0'}])
        client.session.request.return_value = fake_response(status_code=204, content=b'')
        self.assertEqual(client.list_scopes(), [])

    def test_endpoint_list_holds_only_the_dhcp_endpoints(self):
        # "Update PSU Scripts" must never touch another script's endpoints.
        client = build_client()
        client.session.request.return_value = fake_response(json_data=[
            {'id': 1, 'url': '/api/dhcp/scopes'},
            {'id': 2, 'url': '/api/other/thing'},
        ])
        self.assertEqual([ep['id'] for ep in client.get_dhcp_endpoints()], [1])


def _items(n):
    return [{'scope_id': '10.0.1.0', 'ip_address': f'10.0.1.{i}'} for i in range(n)]


def _ok_response(items):
    return fake_response(json_data={'results': [
        {'scope_id': i['scope_id'], 'ip_address': i['ip_address'], 'status': 'ok'} for i in items
    ]})


class ReservationBatchTests(SimpleTestCase):

    def test_each_method_sends_the_list_to_the_reservations_root(self):
        for name, method in (
            ('create_reservations', 'POST'),
            ('update_reservations', 'PUT'),
            ('delete_reservations', 'DELETE'),
        ):
            with self.subTest(name):
                client = build_client()
                items = _items(2)
                client.session.request.return_value = _ok_response(items)
                results = getattr(client, name)(items)
                args, kwargs = client.session.request.call_args
                self.assertEqual(tuple(args[:2]), (method, BASE + '/api/dhcp/reservations'))
                self.assertEqual(kwargs['json'], items)
                self.assertEqual([r['status'] for r in results], ['ok', 'ok'])

    def test_empty_list_makes_no_call(self):
        client = build_client()
        self.assertEqual(client.update_reservations([]), [])
        client.session.request.assert_not_called()

    def test_chunks_of_100_and_results_combined_in_order(self):
        client = build_client()
        items = _items(250)
        client.session.request.side_effect = lambda method, url, json, **kw: _ok_response(json)
        results = client.delete_reservations(items)
        sizes = [len(c.kwargs['json']) for c in client.session.request.call_args_list]
        self.assertEqual(sizes, [100, 100, 50])
        self.assertEqual([r['ip_address'] for r in results], [i['ip_address'] for i in items])

    def test_failed_call_marks_that_chunk_and_the_rest_as_errors(self):
        client = build_client()
        items = _items(250)
        client.session.request.side_effect = [
            _ok_response(items[:100]),
            fake_response(status_code=500, content=b'boom'),
        ]
        results = client.update_reservations(items)
        self.assertEqual(client.session.request.call_count, 2)  # stops after the failure
        self.assertEqual(len(results), 250)
        self.assertEqual({r['status'] for r in results[:100]}, {'ok'})
        self.assertEqual({r['status'] for r in results[100:]}, {'error'})
        self.assertIn('HTTP 500', results[100]['error'])

    def test_old_script_reply_is_reported_as_errors(self):
        # An old script's single-object POST reply has no 'results' list.
        client = build_client()
        client.session.request.return_value = fake_response(json_data={'ip_address': '10.0.1.0'})
        self.assertEqual(client.create_reservations(_items(1))[0]['status'], 'error')

    def test_missing_results_are_padded(self):
        client = build_client()
        items = _items(3)
        client.session.request.return_value = _ok_response(items[:2])
        results = client.update_reservations(items)
        self.assertEqual([r['status'] for r in results], ['ok', 'ok', 'error'])
        self.assertEqual(results[2]['ip_address'], '10.0.1.2')


def build_self_signed_der(cn='dhcp.example.com', sans=('dhcp.example.com',)):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc))
        .not_valid_after(datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, cert.public_bytes(serialization.Encoding.DER)


class FetchCertificateTests(SimpleTestCase):
    """The socket and TLS layers are mocked; a local self-signed certificate is fed through."""

    def _fetch(self, der):
        ssock = mock.MagicMock()
        ssock.getpeercert.return_value = der
        wrap_cm = mock.MagicMock()
        wrap_cm.__enter__.return_value = ssock
        with mock.patch('netbox_windows_dhcp.cert_utils.socket.create_connection'), \
                mock.patch('ssl.SSLContext.wrap_socket', return_value=wrap_cm):
            return fetch_cert_info('dhcp.example.com', 443)

    def test_reads_the_certificate_details(self):
        cert, der = build_self_signed_der()
        info = self._fetch(der)
        self.assertEqual((info['subject_cn'], info['issuer_cn']), ('dhcp.example.com', 'dhcp.example.com'))
        self.assertEqual(info['sans'], ['dhcp.example.com'])
        self.assertEqual(info['not_after'], datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc))
        self.assertTrue(info['pem'].startswith('-----BEGIN CERTIFICATE-----'))
        expected = cert.fingerprint(hashes.SHA256()).hex().upper()
        self.assertEqual(info['fingerprint'], ':'.join(expected[i:i + 2] for i in range(0, 64, 2)))

    def test_no_certificate_raises(self):
        with self.assertRaises(ValueError):
            self._fetch(None)
