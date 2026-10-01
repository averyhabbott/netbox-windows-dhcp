"""
Sample data for the test suite: the fake PSU server and the replies it gives.

``FakePSUClient`` stands in for ``api_client.PSUClient`` so no test ever opens a
network connection. The plugin's import/sync helpers take a ``client`` argument, so
the fake is passed in directly; ``base.run_sync`` patches it in for a whole server sync.
"""

from ..constants import PSU_SCRIPT_VERSION




# ---------------------------------------------------------------------------
# Fake PSU client + canned payloads (offline — no network)
# ---------------------------------------------------------------------------

class FakePSUClient:
    """
    Stand-in for ``api_client.PSUClient`` that returns pre-seeded data and records
    the writes it was asked to make. Construct with whatever the test needs:

        client = FakePSUClient(
            scopes=[...], leases={scope_id: [...]}, reservations={scope_id: [...]},
            exclusions={scope_id: [...]}, scope_options={scope_id: [...]},
            failover=[...],
        )

    Recorded writes are available on ``created_reservations``,
    ``created_exclusions``, ``deleted_exclusions``, ``created_scopes``,
    ``updated_scopes``, ``deleted_scopes``, ``replicated_failover_calls`` for
    assertions.
    ``list_scopes_calls``/``list_leases_calls``/``list_reservations_calls`` record the
    arguments passed on each call (``scope_id=None`` means bulk mode was used).
    Pass ``leases_error``/``reservations_error`` (a ``PSUClientError`` instance) to make
    the corresponding bulk call raise, for testing failure handling;
    ``scope_options_error`` does the same for the per-scope ``list_scope_options``.
    ``list_scopes`` returns the seeded scopes as given: seed them without ``router`` to
    act like a newer PSU script asked for ``include_router=False``, or with it to act
    like an older script that ignores that. ``list_scopes_include_router`` records it.
    """

    def __init__(self, scopes=None, leases=None, reservations=None,
                 exclusions=None, scope_options=None, failover=None,
                 health=None, leases_error=None, reservations_error=None,
                 exclusions_error=None, options_error=None, scope_options_error=None,
                 ping_read_error=None, ping_write_error=None, reservation_results=None):
        self._scopes = scopes or []
        self._leases = leases or {}
        self._reservations = reservations or {}
        self._exclusions = exclusions or {}
        self._scope_options = scope_options or {}
        self._failover = failover or []
        self._health = health or {'version': PSU_SCRIPT_VERSION}
        # Set to a PSUClientError instance to make the corresponding bulk call raise,
        # for testing failure handling (a fetch failure must never drive a deletion).
        self._leases_error = leases_error
        self._reservations_error = reservations_error
        self._exclusions_error = exclusions_error
        self._options_error = options_error
        self._scope_options_error = scope_options_error
        # Set to a PSUClientError instance (status_code=403 for read-only, anything
        # else for an unclassifiable failure) to make ping_write() raise.
        self._ping_read_error = ping_read_error
        self._ping_write_error = ping_write_error
        # {ip_address: result dict} — overrides the 'ok' result a bulk reservation call
        # returns for that IP (e.g. {'status': 'error', 'error': '...'}), which then isn't recorded.
        self._reservation_results = reservation_results or {}

        self.created_reservations = []
        self.updated_reservations = []
        self.deleted_reservations = []
        self.created_exclusions = []
        self.deleted_exclusions = []
        self.created_scopes = []
        self.updated_scopes = []
        self.deleted_scopes = []
        self.list_scopes_calls = []
        self.list_scopes_include_router = []
        self.list_leases_calls = []
        self.list_reservations_calls = []
        self.replicated_failover_calls = []
        # (method, [ip_address, ...]) per bulk reservation call, in call order.
        self.reservation_calls = []

    # --- reads ---
    def ping_read(self):
        if self._ping_read_error is not None:
            raise self._ping_read_error
        return self._health

    def ping_write(self):
        if self._ping_write_error is not None:
            raise self._ping_write_error
        return True

    def list_scopes(self, active_only=False, include_router=True):
        self.list_scopes_calls.append(active_only)
        self.list_scopes_include_router.append(include_router)
        return list(self._scopes)

    def get_scope(self, scope_id):
        from ..api_client import PSUClientError
        for s in self._scopes:
            sid = s.get('scope_id') or s.get('ScopeId') or s.get('network_address')
            if sid == scope_id:
                return s
        raise PSUClientError(f'Scope {scope_id} not found', status_code=404)

    def list_leases(self, scope_id=None):
        self.list_leases_calls.append(scope_id)
        if self._leases_error is not None:
            raise self._leases_error
        if scope_id is None:
            # Bulk mode: real PSU returns a grouped dict when ?format=grouped is passed.
            return dict(self._leases)
        return list(self._leases.get(scope_id, []))

    def list_reservations(self, scope_id=None):
        self.list_reservations_calls.append(scope_id)
        if self._reservations_error is not None:
            raise self._reservations_error
        if scope_id is None:
            # Bulk mode: grouped dict (matches ?format=grouped behavior).
            return dict(self._reservations)
        return list(self._reservations.get(scope_id, []))

    def list_exclusions(self, scope_id):
        return list(self._exclusions.get(scope_id, []))

    def list_all_exclusions(self):
        if self._exclusions_error is not None:
            raise self._exclusions_error
        return dict(self._exclusions)

    def list_scope_options(self, scope_id):
        if self._scope_options_error is not None:
            raise self._scope_options_error
        return list(self._scope_options.get(scope_id, []))

    def list_all_scope_options(self):
        if self._options_error is not None:
            raise self._options_error
        return dict(self._scope_options)

    def list_failover(self):
        return list(self._failover)

    # --- writes (recorded) ---
    def create_reservation(self, payload):
        self.created_reservations.append(payload)
        return payload

    def _batch(self, method, items, record):
        self.reservation_calls.append((method, [i.get('ip_address') for i in items]))
        results = []
        for i in items:
            result = {'scope_id': i.get('scope_id'), 'ip_address': i.get('ip_address'), 'status': 'ok'}
            override = self._reservation_results.get(i.get('ip_address'))
            if override:
                result.update(override)
            else:
                record.append(i)
            results.append(result)
        return results

    def create_reservations(self, items):
        return self._batch('POST', items, self.created_reservations)

    def update_reservations(self, items):
        return self._batch('PUT', items, self.updated_reservations)

    def delete_reservations(self, items):
        return self._batch('DELETE', items, self.deleted_reservations)

    def create_exclusion(self, payload):
        self.created_exclusions.append(payload)
        return payload

    def delete_exclusion(self, payload):
        self.deleted_exclusions.append(payload)

    def create_scope(self, payload):
        self.created_scopes.append(payload)
        return payload

    def update_scope(self, scope_id, payload):
        self.updated_scopes.append((scope_id, payload))
        return payload

    def delete_scope(self, scope_id):
        self.deleted_scopes.append(scope_id)

    def replicate_failover(self, scope_ids):
        self.replicated_failover_calls.append(list(scope_ids))
        return {'replicated': list(scope_ids)}


# Canned PSU response payloads (snake_case, the primary contract documented in
# api_client.py). PascalCase variants are produced inline by the dual-format test.
FAKE_SCOPE_SNAKE = {
    'scope_id': '10.0.1.0',
    'name': 'Building A',
    'start_ip': '10.0.1.10',
    'end_ip': '10.0.1.254',
    'subnet_mask': '255.255.255.0',
    'router': '10.0.1.1',
    'lease_duration_seconds': 86400,
}

FAKE_SCOPE_PASCAL = {
    'ScopeId': '10.0.1.0',
    'Name': 'Building A',
    'StartRange': '10.0.1.10',
    'EndRange': '10.0.1.254',
    'SubnetMask': '255.255.255.0',
    'Router': '10.0.1.1',
    'LeaseDuration': 86400,
}

FAKE_LEASE = {
    'ip_address': '10.0.1.50',
    'client_id': '00-11-22-33-44-55',
    'hostname': 'desktop-abc',
    'scope_id': '10.0.1.0',
    'lease_expiry': '2030-01-01T00:00:00Z',
    'address_state': 'Active',
}

FAKE_RESERVATION = {
    'ip_address': '10.0.1.100',
    'client_id': 'aa-bb-cc-dd-ee-ff',
    'name': 'printer-01',
    'description': '',
    'type': 'Dhcp',
    'scope_id': '10.0.1.0',
}

FAKE_EXCLUSION = {
    'scope_id': '10.0.1.0',
    'start_ip': '10.0.1.200',
    'end_ip': '10.0.1.210',
}


def lease(address, **fields):
    return {**FAKE_LEASE, 'ip_address': address, **fields}


def reservation(address, **fields):
    return {**FAKE_RESERVATION, 'ip_address': address, **fields}
