"""
PSU (PowerShell Universal) DHCP API client.

All PSU endpoints are expected at:
  {scheme}://{hostname}:{port}/api/dhcp/...

Authentication: PSU v5 App Token sent as  Authorization: Bearer <token>.

Expected response shapes are documented in the plan.  PSU scripts on the Windows
DHCP server must be implemented to match this contract.
"""

import logging
import ssl
import tempfile
from typing import Any, Dict, List, Optional

import requests
import requests.adapters
from requests.exceptions import RequestException

from .constants import PSU_SCRIPT_VERSION

logger = logging.getLogger('netbox_windows_dhcp')

# Default timeout (seconds) for PSU API calls
REQUEST_TIMEOUT = 30

# Items per bulk reservation call — keeps each call well inside REQUEST_TIMEOUT.
RESERVATION_BATCH_SIZE = 100


def _batch_error(item: Dict, message: str) -> Dict:
    return {
        'scope_id': item.get('scope_id', ''),
        'ip_address': item.get('ip_address', ''),
        'status': 'error',
        'error': message,
    }


class _SSLContextAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that uses a caller-supplied SSLContext for certificate pinning."""

    def __init__(self, ssl_context, **kwargs):
        self._ssl_context = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, num_pools, maxsize, block=False, **connection_pool_kw):
        connection_pool_kw['ssl_context'] = self._ssl_context
        connection_pool_kw['assert_hostname'] = False
        super().init_poolmanager(num_pools, maxsize, block, **connection_pool_kw)


class PSUClientError(Exception):
    """Raised when the PSU API returns an unexpected response."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class PSUClient:
    """Thin HTTP client for a single PowerShell Universal DHCP API endpoint."""

    def __init__(self, server):
        """
        :param server: DHCPServer model instance
        """
        self.server = server
        self.base_url = server.base_url
        self._cert_tempfile = None  # keeps NamedTemporaryFile alive for the session lifetime
        self.session = requests.Session()
        self._configure_ssl()
        api_key = self._get_api_key()
        if api_key:
            # PSU v5 uses JWT App Tokens passed as Bearer tokens.
            # Strip whitespace in case the token was copy-pasted with trailing newlines/spaces.
            self.session.headers['Authorization'] = f'Bearer {api_key.strip()}'
        self.session.headers['Accept'] = 'application/json'
        self.session.headers['Content-Type'] = 'application/json'

    def _get_api_key(self) -> str:
        """Return the effective API key, preferring any PLUGINS_CONFIG override."""
        from django.conf import settings as django_settings
        override = (
            getattr(django_settings, 'PLUGINS_CONFIG', {})
            .get('netbox_windows_dhcp', {})
            .get('server_overrides', {})
            .get(self.server.hostname, {})
            .get('api_key')
        )
        return override or self.server.api_key

    def _configure_ssl(self):
        """Configure SSL verification on self.session."""
        if not self.server.verify_ssl:
            self.session.verify = False
            return

        ca_cert = getattr(self.server, 'ca_cert', '')
        if not ca_cert:
            self.session.verify = True
            return

        ca_cert_expiry = getattr(self.server, 'ca_cert_expiry', None)
        if ca_cert_expiry:
            from django.utils import timezone
            if ca_cert_expiry < timezone.now():
                raise PSUClientError(
                    f'The stored CA certificate for "{self.server.name}" expired on '
                    f'{ca_cert_expiry.date()}. '
                    f'Re-import it from the Server detail page in NetBox.'
                )

        # Write PEM to a temp file that lives as long as this PSUClient instance.
        self._cert_tempfile = tempfile.NamedTemporaryFile(suffix='.pem', mode='w', delete=True)
        self._cert_tempfile.write(ca_cert)
        self._cert_tempfile.flush()

        # Pinned-cert trust model: we verify the server presents exactly this cert
        # (CERT_REQUIRED + load_verify_locations), but skip hostname checking because
        # self-signed PSU certs are commonly issued for 'localhost' regardless of the
        # server's actual FQDN.  VERIFY_X509_PARTIAL_CHAIN (Python 3.10+) lets a
        # non-CA leaf cert act as a trust anchor without a full issuer chain.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False  # must be set before verify_mode
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        ctx.load_verify_locations(self._cert_tempfile.name)

        self.session.mount('https://', _SSLContextAdapter(ctx))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f'{self.base_url}/{path.lstrip("/")}'

    @property
    def mgmt_base_url(self) -> str:
        """Base URL for the PSU management API (distinct from the DHCP endpoint base)."""
        return f'{self.base_url.rsplit("/api/dhcp", 1)[0]}/api/v1'

    def _mgmt_url(self, path: str) -> str:
        return f'{self.mgmt_base_url}/{path.lstrip("/")}'

    def _mgmt_request(self, method: str, path: str, **kwargs) -> Any:
        url = self._mgmt_url(path)
        try:
            response = self.session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            if not response.ok:
                raise PSUClientError(
                    f'{method} {url} returned HTTP {response.status_code}: {response.text}',
                    status_code=response.status_code,
                )
            if not response.content:
                return None
            return response.json()
        except PSUClientError:
            raise
        except RequestException as exc:
            raise PSUClientError(f'Network error calling {url}: {exc}') from exc
        except ValueError as exc:
            raise PSUClientError(f'Invalid JSON response from {url}: {exc}') from exc

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = self._url(path)
        try:
            response = self.session.request(
                method, url, timeout=REQUEST_TIMEOUT, **kwargs
            )
            if response.status_code == 204:
                return None
            if not response.ok:
                raise PSUClientError(
                    f'{method} {url} returned HTTP {response.status_code}: {response.text}',
                    status_code=response.status_code,
                )
            return response.json()
        except PSUClientError:
            raise
        except RequestException as exc:
            raise PSUClientError(f'Network error calling {url}: {exc}') from exc
        except ValueError as exc:
            raise PSUClientError(f'Invalid JSON response from {url}: {exc}') from exc

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        return self._request('GET', path, params=params)

    def _get_list(self, path: str, params: Optional[Dict] = None) -> List[Dict]:
        """Like _get but always returns a list, guarding against single-object JSON responses."""
        result = self._request('GET', path, params=params)
        if result is None:
            return []
        if isinstance(result, dict):
            return [result]
        return result

    def _post(self, path: str, data: Dict) -> Any:
        return self._request('POST', path, json=data)

    def _put(self, path: str, data: Dict) -> Any:
        return self._request('PUT', path, json=data)

    def _delete(self, path: str) -> None:
        self._request('DELETE', path)

    # ------------------------------------------------------------------
    # Scopes
    # ------------------------------------------------------------------

    def list_scopes(self, active_only: bool = False, include_router: bool = True) -> List[Dict]:
        """
        Return all DHCP scopes on this server.

        include_router=False asks the PSU script to leave out each scope's "router"
        (one Windows call per scope saved); the caller reads Option 3 from the options
        data instead. Older scripts ignore it and still send "router", so a reply
        without the key means the router has to come from the options.
        """
        params = {}
        if active_only:
            params['active_only'] = 'true'
        if not include_router:
            params['include_router'] = 'false'
        return self._get_list('scopes', params=params or None)

    def get_scope(self, scope_id: str) -> Dict:
        return self._get(f'scopes/{scope_id}')

    def create_scope(self, payload: Dict) -> Dict:
        """
        Expected payload:
            {
              "scope_id": "10.0.1.0",        # network address
              "name": "Building A",
              "start_ip": "10.0.1.10",
              "end_ip": "10.0.1.254",
              "subnet_mask": "255.255.255.0",
              "router": "10.0.1.1",          # optional
              "lease_duration_seconds": 86400,
              "description": ""
            }
        """
        return self._post('scopes', payload)

    def update_scope(self, scope_id: str, payload: Dict) -> Dict:
        return self._put(f'scopes/{scope_id}', payload)

    def delete_scope(self, scope_id: str) -> None:
        self._delete(f'scopes/{scope_id}')

    # ------------------------------------------------------------------
    # Leases
    # ------------------------------------------------------------------

    def list_leases(self, scope_id: Optional[str] = None):
        """
        Return active DHCP leases.

        With scope_id: returns List[Dict] (flat list for that scope).
        Without scope_id: passes ?format=grouped and returns Dict[str, List[Dict]]
        keyed by scope_id — one bulk call instead of one per scope.
        """
        if scope_id:
            return self._get_list('leases', params={'scope_id': scope_id})
        result = self._get('leases', params={'format': 'grouped'})
        if isinstance(result, list) and result:
            raise PSUClientError(
                'Bulk leases fetch returned a flat list — PSU script may be too old to support '
                f'?format=grouped; run Update PSU Scripts to install PSU script v{PSU_SCRIPT_VERSION}'
            )
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # Reservations
    # ------------------------------------------------------------------

    def list_reservations(self, scope_id: Optional[str] = None):
        """
        Return DHCP reservations.

        With scope_id: returns List[Dict] (flat list for that scope).
        Without scope_id: passes ?format=grouped and returns Dict[str, List[Dict]]
        keyed by scope_id — one bulk call instead of one per scope.
        """
        if scope_id:
            return self._get_list('reservations', params={'scope_id': scope_id})
        result = self._get('reservations', params={'format': 'grouped'})
        if isinstance(result, list) and result:
            raise PSUClientError(
                'Bulk reservations fetch returned a flat list — PSU script may be too old to support '
                f'?format=grouped; run Update PSU Scripts to install PSU script v{PSU_SCRIPT_VERSION}'
            )
        return result if isinstance(result, dict) else {}

    def create_reservation(self, payload: Dict) -> Dict:
        """
        Expected payload:
            {
              "scope_id": "10.0.1.0",
              "ip_address": "10.0.1.100",
              "client_id": "00-11-22-33-44-55",
              "name": "printer-01",
              "description": "",
              "type": "Dhcp"   # "Dhcp", "Bootp", or "Both"
            }
        """
        return self._post('reservations', payload)

    def create_reservations(self, items: List[Dict]) -> List[Dict]:
        """
        Bulk create. Each item has the same keys as create_reservation().
        Returns one result per item, in order (see _reservation_batch).
        """
        return self._reservation_batch('POST', items)

    def update_reservations(self, items: List[Dict]) -> List[Dict]:
        """
        Bulk update, each reservation found by scope_id + ip_address.
        Item keys: scope_id, ip_address (required); client_id, name,
        description, type (optional — only the keys sent are changed).
        Returns one result per item, in order (see _reservation_batch).
        """
        return self._reservation_batch('PUT', items)

    def delete_reservations(self, items: List[Dict]) -> List[Dict]:
        """
        Bulk delete. Items: {"scope_id": ..., "ip_address": ...}.
        Returns one result per item, in order (see _reservation_batch).
        """
        return self._reservation_batch('DELETE', items)

    def _reservation_batch(self, method: str, items: List[Dict]) -> List[Dict]:
        """
        Send items to /reservations in chunks of RESERVATION_BATCH_SIZE, one call per chunk.

        Returns one result per item, in order:
            {"scope_id", "ip_address", "status": "ok" | "not_found" | "error",
             "error"?, "reservation"?}
        Never raises for a failed call: that chunk and every later one are returned
        as 'error' results carrying the failure, so the caller still gets a result
        for every item and can log each one.
        """
        results = []
        for start in range(0, len(items), RESERVATION_BATCH_SIZE):
            chunk = items[start:start + RESERVATION_BATCH_SIZE]
            try:
                response = self._request(method, 'reservations', json=chunk)
                if not isinstance(response, dict) or not isinstance(response.get('results'), list):
                    raise PSUClientError(
                        f'{method} reservations returned an unexpected response — PSU script '
                        f'may be too old to support bulk reservation calls'
                    )
                chunk_results = response['results'][:len(chunk)]
                for item in chunk[len(chunk_results):]:
                    chunk_results.append(_batch_error(item, 'No result returned for this item'))
                results.extend(chunk_results)
            except PSUClientError as exc:
                results.extend(_batch_error(item, str(exc)) for item in items[start:])
                break
        return results

    # ------------------------------------------------------------------
    # Failover
    # ------------------------------------------------------------------

    def list_failover(self) -> List[Dict]:
        return self._get_list('failover')

    def create_failover(self, payload: Dict) -> Dict:
        """
        Expected payload mirrors Windows DHCP failover parameters:
            {
              "name": "FAILOVER-1",
              "primary_server": "dhcp01.example.com",
              "secondary_server": "dhcp02.example.com",
              "scope_ids": ["10.0.1.0", "10.0.2.0"],
              "mode": "LoadBalance",           # or "HotStandby"
              "max_client_lead_time": 3600,
              "max_response_delay": 30,
              "state_switchover_interval": null,
              "enable_auth": false,
              "shared_secret": ""
            }
        """
        return self._post('failover', payload)

    def replicate_failover(self, scope_ids: List[str]) -> Dict:
        """
        Force failover replication for specific scopes only — faster than
        replicating an entire relationship, since it skips every other scope
        the relationship covers. The server resolves each scope's relationship
        (and partner) internally, so scope IDs from different relationships
        can be batched into a single call.
        """
        return self._post('failover/replicate', {'scope_ids': scope_ids})

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    def list_server_options(self) -> List[Dict]:
        """Return server-level DHCP option values."""
        return self._get_list('options/server')

    def list_scope_options(self, scope_id: str) -> List[Dict]:
        """Return scope-level DHCP option values for a given scope."""
        return self._get_list(f'options/scope/{scope_id}')

    # ------------------------------------------------------------------
    # Exclusion Ranges
    # ------------------------------------------------------------------

    def list_exclusions(self, scope_id: str) -> List[Dict]:
        """
        Return exclusion ranges for the given scope (flat list).
        Used by the push path (_sync_exclusions) — scope_id is required.
        """
        return self._get_list('exclusions', params={'scope_id': scope_id})

    def list_all_exclusions(self) -> Dict:
        """
        Bulk fetch all exclusion ranges across every scope.
        Returns Dict[str, List[Dict]] keyed by scope_id.
        Used by the pull path — one call instead of one per scope.
        """
        result = self._get('exclusions')
        return result if isinstance(result, dict) else {}

    def list_all_scope_options(self) -> Dict:
        """
        Bulk fetch all scope-level option values across every scope.
        Returns Dict[str, List[Dict]] keyed by scope_id (the scope_options
        key from the GET /api/dhcp/options envelope).
        Used by the pull path — one call instead of one per scope.
        """
        result = self._get('options')
        if not isinstance(result, dict):
            return {}
        scope_options = result.get('scope_options', {})
        return scope_options if isinstance(scope_options, dict) else {}

    def create_exclusion(self, payload: Dict) -> Dict:
        """
        Create an exclusion range on a DHCP scope.

        Expected payload:
            {
              "scope_id":  "10.0.1.0",
              "start_ip":  "10.0.1.50",
              "end_ip":    "10.0.1.59"
            }
        """
        return self._post('exclusions', payload)

    def delete_exclusion(self, payload: Dict) -> None:
        """
        Delete an exclusion range identified by scope_id + start_ip + end_ip.

        Windows DHCP has no per-exclusion ID; the 3-tuple uniquely identifies it.
        Expected payload:
            {
              "scope_id":  "10.0.1.0",
              "start_ip":  "10.0.1.50",
              "end_ip":    "10.0.1.59"
            }
        """
        self._request('DELETE', 'exclusions', json=payload)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def ping_read(self) -> Dict:
        """
        GET /api/dhcp/health — verify read connectivity and auth.

        Returns the health response dict, which includes a 'version' key
        containing the PSU script version string (e.g. '1.0.0').
        Raises PSUClientError on any failure.
        """
        return self._get('health')

    def ping_write(self) -> bool:
        """
        POST /api/dhcp/health — verify write access.

        Returns True on success, raises PSUClientError on failure.
        A 403 from the endpoint indicates read-only token (DHCPReader role).
        """
        self._post('health', {})
        return True

    # ------------------------------------------------------------------
    # PSU Management API (endpoint record updates)
    # ------------------------------------------------------------------

    def get_dhcp_endpoints(self) -> List[Dict]:
        """
        GET /api/v1/endpoint — return all PSU endpoint records whose URL
        begins with /api/dhcp/.
        """
        all_endpoints = self._mgmt_request('GET', 'endpoint')
        if not isinstance(all_endpoints, list):
            return []
        return [ep for ep in all_endpoints if str(ep.get('url', '')).startswith('/api/dhcp/')]

    def update_endpoint(self, endpoint_obj: Dict) -> None:
        """PUT /api/v1/endpoint/{id} — update a single endpoint's scriptBlock."""
        ep_id = endpoint_obj['id']
        self._mgmt_request('PUT', f'endpoint/{ep_id}', json=endpoint_obj)

    def create_endpoint(self, endpoint_obj: Dict) -> Dict:
        """POST /api/v1/endpoint — register a new endpoint."""
        return self._mgmt_request('POST', 'endpoint', json=endpoint_obj)

    def delete_endpoint(self, ep_id: int) -> None:
        """DELETE /api/v1/endpoint/{id} — remove a registered endpoint."""
        self._mgmt_request('DELETE', f'endpoint/{ep_id}')

    def restart_endpoints(self) -> None:
        """POST /api/v1/endpoint/restart — reload all endpoint definitions."""
        self._mgmt_request('POST', 'endpoint/restart', json={})
