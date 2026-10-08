# Test map

`fixtures.py` and `base.py` hold shared code and run no tests; the tests are in the `test_*.py` files, grouped by feature. Put a new test in the file for the feature it checks; don't start a file per build phase.

| File | What it checks |
| --- | --- |
| `fixtures.py` | Sample data: `FakePSUClient` (the fake PSU server) and its canned replies (`FAKE_*`, `lease`, `reservation`) |
| `base.py` | Helpers: `make_*` builders, `run_sync`, `ui_url`/`api_url`, `make_job`, `error_messages`, `grant` (object permissions), `changes_for` (change log), IP helpers |
| `test_helpers.py` | Small rules: MAC format, lease time display, DNS names, router from options, scope for an IP, failover lookup, PSU version gates, the plugin-write flag |
| `test_models.py` | What NetBox accepts: failover, scope (with or without prefix, uniqueness), exclusion, built-in option codes, settings |
| `test_filtersets.py` | List filters, the Unassigned Scopes saved filter, global search |
| `test_psu_client.py` | Requests sent to PSU, reading replies, reservation batches, reading a server certificate |
| `test_psu_script.py` | The shipped PSU script parses into the endpoints the plugin deploys |
| `test_import.py` | Import from Server |
| `test_sync_ips.py` | IP sync for one scope: create/update IPs, hostnames, lease details, cleanup, tenant, VRF |
| `test_sync_scopes.py` | Scope settings pull/push: options, router, description, active state, failover membership, exclusions, deletes |
| `test_sync_server.py` | A whole server sync: fetch failures, read-only servers, health check, replication, which server handles which scope (guard table) |
| `test_reservations.py` | Reservation push: reconcile, placeholders, the push job, the save/delete signals |
| `test_signals.py` | Scope and exclusion saves/deletes queue push or delete jobs |
| `test_jobs.py` | Job scheduling, who a job runs as, jobs failing or skipping |
| `test_locks.py` | What users are refused: IP lock, range guard, Push Scope Info off (UI and API), option values in use, failovers read-only |
| `test_api.py` | REST API: NetBox's CRUD harness plus API-only behavior |
| `test_views.py` | Web pages: NetBox's view harness, buttons, settings and schedule pages, permissions, lease panel/column, the Leases page |
| `test_commands.py` | Management commands (`dhcp_apply_prefix_tenant`, `dhcp_fix_ip_vrf`) |

## Rules

- A test checks behavior someone relies on: what the plugin does to data, what it sends to a server, what it refuses. Never the code's text, layout or wording.
- Messages: check the facts in them (an IP, a scope name, a count), not the sentence. For a refusal, check that nothing changed and that an error was shown (`error_messages`).
- A test must pass whenever the feature works. No stored counts or snapshots that change with NetBox releases.
- Many cases of one rule go in one test as a table with `subTest`.
- No network: use `FakePSUClient` or mocks.
- Shared code goes in `fixtures.py` (data and the fake server) or `base.py` (helpers), not copied between test files.
