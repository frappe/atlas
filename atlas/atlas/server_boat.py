"""Boat daemon management on a host — the per-host bearer token, the systemd
drop-in that gives the daemon its network listener, and starting the service;
growing to the binary + allow-list + unit install as the `Server` controller is
thinned (spec/33-boat.md).

Extracted from the `Server` controller: standing up and maintaining the boat
daemon on a host is one cohesive reason to change, separate from the Server doc
lifecycle, the ANCP seed-of-trust bootstrap, and the host file/script uploads.
Free functions taking the `Server`, following the `vm_provisioning.py` /
`migration.py` pattern. The controller keeps thin delegators where a caller
reaches these through the doc (tests call `server.mint_boat_token()` /
`server._current_or_minted_boat_token()`; `_install_boat` calls
`self._install_boat_token`). The host SSH still runs through `Server._boat_ssh`,
so the `run_ssh` test mock seam stays on the controller.
"""

from __future__ import annotations

import json
import secrets

import frappe

from atlas.atlas.ssh import ssh_key_file

# Where the daemon reads its bearer token, and the token's lifetimes (spec/33 §12).
# The daemon serves a minted token until BOAT_TOKEN_TTL_DAYS past minting and
# refuses it after (fail-closed); Atlas re-mints within BOAT_TOKEN_REMINT_WITHIN_DAYS
# of that on every bootstrap/upgrade, so a reachable host is always handed a fresh
# token well before the hard expiry — and a leaked-but-unrotated one is still bounded.
BOAT_TOKEN_PATH = "/etc/boat/token"
BOAT_TOKEN_TTL_DAYS = 30
BOAT_TOKEN_REMINT_WITHIN_DAYS = 7


def mint_boat_token(server) -> str:
	"""Mint this host's bearer token and stamp its hard expiry (spec/33 §12).

	Short-lived and per-host. The token is stored ENCRYPTED — a Password field, so
	it lives in __Auth and never in the row or a log — and returned so the caller
	installs the exact value without re-reading it. The expiry is the hard one the
	daemon enforces; the re-mint window (below) means a reachable host is handed a
	fresh token well before it and never reaches it.

	Written with set_encrypted_password + db_set rather than a full server.save():
	this runs mid-bootstrap/upgrade, and re-running every validate/before_save hook
	to stamp one credential is both needless and a way to trip a half-built doc."""
	from frappe.utils.password import set_encrypted_password

	token = secrets.token_urlsafe(32)
	set_encrypted_password(server.doctype, server.name, token, "boat_token")
	# Carry the token on the in-memory doc too: `Document._save_passwords`
	# REMOVES any Password field that is empty at save time, and bootstrap /
	# upgrade_boat both end with `server.save()` — so a mint that only wrote
	# __Auth was silently deleted by the very save that closed the operation,
	# leaving the host's /etc/boat/token the only copy (and the row tokenless
	# on the next read). Setting the attribute makes the trailing save re-write
	# the same value (then mask it), keeping row and host in agreement.
	server.boat_token = token
	server.db_set(
		"boat_token_expires_at",
		frappe.utils.add_to_date(frappe.utils.now_datetime(), days=BOAT_TOKEN_TTL_DAYS),
	)
	return token


def current_or_minted_boat_token(server) -> str:
	"""The stored token if it is present and not yet within the re-mint window of
	its hard expiry, else a freshly minted one. Re-minting early is what keeps a
	reachable host from ever reaching the hard expiry the daemon fails closed on."""
	token = server.get_password("boat_token", raise_exception=False)
	expires = server.boat_token_expires_at
	if token and expires:
		remaining = frappe.utils.get_datetime(expires) - frappe.utils.now_datetime()
		if remaining.days > BOAT_TOKEN_REMINT_WITHIN_DAYS:
			return token
	return mint_boat_token(server)


def install_boat_token(server, connection, key_path) -> None:
	"""Write this host's bearer token to /etc/boat/token, minting or rotating it
	first. The file is the JSON form Boat reads — the token and the hard expiry it
	enforces — installed 0640 root:boat so the daemon user reads it and nobody
	else. The secret is piped on stdin, never argv (§12). _start_boat's restart
	reads it fresh; a rotation with no restart is this same write followed by
	`systemctl reload boat` (the daemon reloads the token on SIGHUP)."""
	token = current_or_minted_boat_token(server)
	payload = json.dumps(
		{
			"token": token,
			"hard_expires_at": frappe.utils.get_datetime(server.boat_token_expires_at)
			.astimezone()
			.isoformat(),
		}
	)
	server._boat_ssh(
		connection,
		key_path,
		"installing the boat token",
		f"sudo install -D -m 0640 -o root -g boat /dev/stdin {BOAT_TOKEN_PATH}",
		stdin=payload,
	)


def boat_dropin_contents(server) -> str:
	"""The single systemd drop-in that fully owns boat's ExecStart.

	It ALWAYS adds `--listen` so the daemon serves its HTTP API on the network.
	Atlas drives every heavy host verb — provision-vm, sync-image, snapshot,
	promote, the s3 backups — over the boat daemon's HTTP listener
	(`scripts_catalog.HTTP_HOST_VERBS`, `boat_client.base_url_for_server`); the
	shipped unit deliberately omits `--listen` (it expects a registration handshake
	to hand over a tunnel address), so without this drop-in boat serves only the
	local unix socket and NONE of those verbs can reach the host. Atlas is the
	operator the unit's comment defers to, so it writes the listener here.

	It also folds in the datum flags when metrics export is configured, because
	two ExecStart-owning drop-ins would fight — one drop-in owns ExecStart.

	The bind is `:<port>` (every interface, i.e. PUBLICLY reachable): the
	controller reaches the host over the network and the per-host bearer token
	(`--token-file`, checked by boat's TunnelHandler) is the auth boundary. Harden
	to the management-tunnel address once that transport is built."""
	port = frappe.conf.get("atlas_boat_port") or 8080
	execstart = (
		"/usr/local/bin/boat daemon --socket /run/boat/boat.sock "
		"--store /var/lib/boat/boat.db --token-file /etc/boat/token "
		f"--listen :{port}"
	)
	url = frappe.conf.get("atlas_datum_url")
	if url:
		execstart += (
			f" --server-name {server.name} --datum-url {url} "
			"--datum-token-file /etc/boat/datum-tokens.json"
		)
	return f"[Service]\nExecStart=\nExecStart={execstart}\n"


def start_boat(server, connection) -> None:
	"""Enable and (re)start boat.service — after the `boat bootstrap` Task.

	The order is not obvious, so: the daemon's first act is to scan the host and
	adopt what it finds (spec/33 §3.4), and on a fresh box there is nothing to
	find until `boat bootstrap` has made /var/lib/atlas, the thin pool and the
	nft scaffold. Starting it earlier points the adoption scan at a host that
	does not exist yet and makes a daemon fault indistinguishable from an
	unbootstrapped host.

	`restart`, not `start`: on a re-bootstrap the unit is already running the
	PREVIOUS binary and `start` would leave it there — the rename in
	_install_boat only takes effect on re-exec. `enable` is separate and
	idempotent, and is what survives a reboot.

	boat-networkd.service is installed but deliberately NOT started: ANCP is
	still served by atlas-networkd on these hosts, and two daemons programming
	one wg-mesh is worse than either.
	"""
	with ssh_key_file(connection.ssh_private_key) as key_path:
		server._boat_ssh(
			connection, key_path, "enabling boat.service", "sudo systemctl enable boat.service"
		)
		server._boat_ssh(
			connection, key_path, "starting boat.service", "sudo systemctl restart boat.service"
		)
		# is-active AFTER the restart: `systemctl restart` returns once the unit
		# has been exec'd (the unit is Type=exec), not once it has settled, so a
		# daemon that dies on its first read of the host exits 0 here.
		server._boat_ssh(
			connection, key_path, "boat.service did not stay up", "sudo systemctl is-active boat.service"
		)
