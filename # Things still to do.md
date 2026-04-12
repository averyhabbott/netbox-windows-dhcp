# Things still to do

~~* Validate authentication (PSU is currently not authenticating on endpoints)~~
~~  * Add to PSU API scripts as commented-out line for easy feature toggle~~
* Test pushing reservations
  * Do reservations need names?
* Test pushing scopes
* Make sure only one instance of an option code exists for a scope
  * Validate this is DHCP spec before implementing
* Overrides for pushing reservations, scope info, and active/passive
* Find better way to deal with staging names/IPs for OOBM networks and similar
  * Current logic would delete staged info if there was no active lease/reservation
  * Can't stage a reservation without client ID, right?
