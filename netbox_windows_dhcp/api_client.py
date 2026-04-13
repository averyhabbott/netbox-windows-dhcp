"""
PSU (PowerShell Universal) DHCP API client.

All PSU endpoints are expected at:
  {scheme}://{hostname}:{port}/api/dhcp/...

Authentication: PSU v5 App Token sent as  Authorization: Bearer <token>.

Expected response shapes are documented in the plan.  PSU scripts on the Windows
DHCP server must be implemented to match this contract.
"""

import logging
from typing import Any, Dict, List, Optional

import requests
from requests.exceptions import RequestException

logger = logging.getLogger('netbox_windows_dhcp')

# Default timeout (seconds) for PSU API calls
REQUEST_TIMEOUT = 30


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
        self.session = requests.Session()
        self.session.verify = server.verify_ssl
        if server.api_key:
            # PSU v5 uses JWT App Tokens passed as Bearer tokens.
            # Strip whitespace in case the token was copy-pasted with trailing newlines/spaces.
            self.session.headers['Authorization'] = f'Bearer {server.api_key.strip()}'
        self.session.headers['Accept'] = 'application/json'
        self.session.headers['Content-Type'] = 'application/json'

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f'{self.base_url}/{path.lstrip("/")}'

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

    def list_scopes(self) -> List[Dict]:
        """Return all DHCP scopes on this server."""
        return self._get_list('scopes')

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

    # ------------------------------------------------------------------
    # Leases
    # ------------------------------------------------------------------

    def list_leases(self, scope_id: Optional[str] = None) -> List[Dict]:
        """
        Return active DHCP leases.  Filter by scope_id if provided.

        Each lease dict:
            {
              "ip_address": "10.0.1.50",
              "client_id": "00-11-22-33-44-55",
              "hostname": "DESKTOP-ABC123",
              "scope_id": "10.0.1.0",
              "lease_expiry": "2026-04-12T00:00:00Z",
              "address_state": "Active"
            }
        """
        params = {'scope_id': scope_id} if scope_id else {}
        return self._get_list('leases', params=params)

    # ------------------------------------------------------------------
    # Reservations
    # ------------------------------------------------------------------

    def list_reservations(self, scope_id: Optional[str] = None) -> List[Dict]:
        """
        Return DHCP reservations.

        Each reservation dict:
            {
              "ip_address": "10.0.1.100",
              "client_id": "00-11-22-33-44-55",
              "name": "printer-01",
              "description": "",
              "type": "Dhcp"
            }
        """
        params = {'scope_id': scope_id} if scope_id else {}
        return self._get_list('reservations', params=params)

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

    def update_reservation(self, client_id: str, payload: Dict) -> Dict:
        return self._put(f'reservations/{client_id}', payload)

    def delete_reservation(self, client_id: str) -> None:
        self._delete(f'reservations/{client_id}')

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
        Return exclusion ranges for the given scope.

        Each exclusion dict:
            {
              "scope_id": "10.0.1.0",
              "start_ip": "10.0.1.50",
              "end_ip":   "10.0.1.59"
            }
        """
        return self._get_list('exclusions', params={'scope_id': scope_id})

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
