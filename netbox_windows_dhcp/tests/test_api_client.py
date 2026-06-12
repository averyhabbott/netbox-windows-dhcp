"""
PSUClient tests. Fully offline: the requests.Session is replaced with a mock,
so no socket is ever opened.
"""

from unittest import mock

from django.test import SimpleTestCase, override_settings

from ..api_client import PSUClient, PSUClientError
from ..models import DHCPServer


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
    server = DHCPServer(**defaults)
    client = PSUClient(server)
    client.session = mock.Mock()
    return client


class URLConstructionTests(SimpleTestCase):
    def test_base_url(self):
        client = build_client()
        self.assertEqual(client.base_url, 'https://dhcp.example.com:443/api/dhcp')

    def test_list_scopes_url_and_method(self):
        client = build_client()
        client.session.request.return_value = fake_response(json_data=[])
        client.list_scopes()
        method, url = client.session.request.call_args[0][:2]
        self.assertEqual(method, 'GET')
        self.assertEqual(url, 'https://dhcp.example.com:443/api/dhcp/scopes')

    def test_get_scope_url(self):
        client = build_client()
        client.session.request.return_value = fake_response(json_data={'scope_id': '10.0.1.0'})
        client.get_scope('10.0.1.0')
        method, url = client.session.request.call_args[0][:2]
        self.assertEqual(method, 'GET')
        self.assertEqual(url, 'https://dhcp.example.com:443/api/dhcp/scopes/10.0.1.0')

    def test_create_reservation_posts_json(self):
        client = build_client()
        client.session.request.return_value = fake_response(json_data={})
        payload = {'scope_id': '10.0.1.0', 'ip_address': '10.0.1.100'}
        client.create_reservation(payload)
        args, kwargs = client.session.request.call_args
        self.assertEqual(args[0], 'POST')
        self.assertEqual(args[1], 'https://dhcp.example.com:443/api/dhcp/reservations')
        self.assertEqual(kwargs['json'], payload)

    def test_delete_exclusion_sends_json_body(self):
        client = build_client()
        client.session.request.return_value = fake_response(status_code=204, content=b'')
        payload = {'scope_id': '10.0.1.0', 'start_ip': '10.0.1.5', 'end_ip': '10.0.1.9'}
        client.delete_exclusion(payload)
        args, kwargs = client.session.request.call_args
        self.assertEqual(args[0], 'DELETE')
        self.assertEqual(args[1], 'https://dhcp.example.com:443/api/dhcp/exclusions')
        self.assertEqual(kwargs['json'], payload)

    def test_mgmt_url_for_endpoints(self):
        client = build_client()
        client.session.request.return_value = fake_response(json_data=[])
        client.get_dhcp_endpoints()
        method, url = client.session.request.call_args[0][:2]
        self.assertEqual(method, 'GET')
        self.assertEqual(url, 'https://dhcp.example.com:443/api/v1/endpoint')


class ResponseHandlingTests(SimpleTestCase):
    def test_204_returns_none(self):
        client = build_client()
        client.session.request.return_value = fake_response(status_code=204, content=b'')
        self.assertIsNone(client.delete_reservation('aa-bb'))

    def test_error_raises_with_status_code(self):
        client = build_client()
        client.session.request.return_value = fake_response(status_code=403, content=b'forbidden')
        with self.assertRaises(PSUClientError) as ctx:
            client.ping_write()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_get_list_wraps_single_dict(self):
        client = build_client()
        client.session.request.return_value = fake_response(json_data={'scope_id': '10.0.1.0'})
        result = client.list_scopes()
        self.assertEqual(result, [{'scope_id': '10.0.1.0'}])

    def test_get_list_none_becomes_empty(self):
        client = build_client()
        client.session.request.return_value = fake_response(status_code=204, content=b'')
        self.assertEqual(client.list_scopes(), [])

    def test_ping_read_returns_payload(self):
        client = build_client()
        client.session.request.return_value = fake_response(json_data={'version': '1.0.2'})
        self.assertEqual(client.ping_read(), {'version': '1.0.2'})

    def test_get_dhcp_endpoints_filters_by_prefix(self):
        client = build_client()
        client.session.request.return_value = fake_response(json_data=[
            {'id': 1, 'url': '/api/dhcp/scopes'},
            {'id': 2, 'url': '/api/other/thing'},
        ])
        result = client.get_dhcp_endpoints()
        self.assertEqual([ep['id'] for ep in result], [1])


class APIKeyResolutionTests(SimpleTestCase):
    def test_model_api_key_used_by_default(self):
        client = build_client(api_key='model-token')
        self.assertEqual(client._get_api_key(), 'model-token')

    @override_settings(PLUGINS_CONFIG={'netbox_windows_dhcp': {
        'server_overrides': {'dhcp.example.com': {'api_key': 'override-token'}},
    }})
    def test_plugins_config_override_wins(self):
        client = build_client(api_key='model-token')
        self.assertEqual(client._get_api_key(), 'override-token')


class SSLConfigTests(SimpleTestCase):
    def test_verify_disabled_sets_session_verify_false(self):
        # Build without the post-construction session swap so we inspect the real session.
        server = DHCPServer(name='S', hostname='h.example.com', verify_ssl=False)
        client = PSUClient(server)
        self.assertFalse(client.session.verify)

    def test_verify_enabled_no_cert_sets_verify_true(self):
        server = DHCPServer(name='S', hostname='h.example.com', verify_ssl=True, ca_cert='')
        client = PSUClient(server)
        self.assertTrue(client.session.verify)
