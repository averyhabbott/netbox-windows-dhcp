PSU_SCRIPT_VERSION = '2.0.1'

# Lowest PSU script version that has the reservation update/delete-by-scope-and-IP
# endpoints. Servers running an older script get create-only reservation pushes.
PSU_RESERVATION_BY_IP_MIN_VERSION = '2.0.0'

# Lowest PSU script version whose scope create/update endpoints accept `state`
# (Active/InActive). Older scripts ignore it, so the sync neither pushes a scope's
# active state to them nor counts it as a difference (it is still pulled).
PSU_SCOPE_STATE_MIN_VERSION = '2.0.0'
