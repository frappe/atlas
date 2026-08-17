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

# Host paths. The binary is staged in its OWN directory (see install_boat step 3
# for why the rename has to stay on one filesystem); the rest stage under
# /var/lib/atlas/boat, where they also serve as the record of what was installed.
BOAT_BINARY = "/usr/local/bin/boat"
BOAT_INCOMING_BINARY = f"{BOAT_BINARY}.incoming"
BOAT_STAGING_DIRECTORY = "/var/lib/atlas/boat"
BOAT_ARTIFACTS: list[tuple[str, str]] = [
	("bin/boat", BOAT_INCOMING_BINARY),
	("sudoers.d/boat", f"{BOAT_STAGING_DIRECTORY}/sudoers"),
	("systemd/boat.service", f"{BOAT_STAGING_DIRECTORY}/boat.service"),
	("systemd/boat-networkd.service", f"{BOAT_STAGING_DIRECTORY}/boat-networkd.service"),
]

# Host paths for the boat systemd drop-in and the datum token bundle.
DATUM_TOKENS_PATH = "/etc/boat/datum-tokens.json"
BOAT_DROPIN_PATH = "/etc/systemd/system/boat.service.d/10-boat.conf"


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


def install_boat(server, connection) -> None:
	"""Install the boat binary, its allow-list, its service user and its units —
	the step that makes `boat <verb>` a command this host has. Runs after the
	upload (which staged all four artifacts) and before install.sh, whose last
	gate is `command -v boat`.

	THE ORDER IS THE POINT, and it is deliberately not the boat README's:

	  1. the `boat` system user. Both units run as it and every line of the
	     allow-list grants to it by name, so nothing below means anything until
	     it exists. Idempotent: a re-bootstrap finds it and adds nothing.
	  2. `visudo -cf` the STAGED allow-list, and only then `install` it 0440
	     root:root. Validate-then-install, never the README's reverse: a sudoers
	     file sudo cannot parse does not merely disable boat's grants, it takes
	     out the whole /etc/sudoers.d directory — the boat user ends up with no
	     grants at all and every verb on the host fails at once. Checking the
	     staged copy means an invalid file never reaches /etc.
	  3. the binary, renamed into place from a staging name in the SAME
	     directory. `mv` within one filesystem is rename(2), so a process that
	     execs /usr/local/bin/boat mid-install gets the whole old inode or the
	     whole new one and never a half-written file — and a running daemon
	     keeps its own open inode until it is restarted. That is
	     internal/update's Install reasoning, and the same reason it stages
	     inside /usr/local/bin rather than in /tmp.
	  4. the two units, then `daemon-reload`: systemd has to be told about a
	     unit file before anything asks it to start one.

	Starting boat.service is NOT here — see start_boat for why it waits.
	"""
	digest = server._staged_boat_digest()
	steps = [
		(
			"creating the boat service user",
			"id boat >/dev/null 2>&1 || sudo useradd --system --home-dir /var/lib/boat "
			"--shell /usr/sbin/nologin boat",
		),
		("checking the sudoers allow-list", f"sudo visudo -cf {BOAT_STAGING_DIRECTORY}/sudoers"),
		(
			"installing the sudoers allow-list",
			f"sudo install -m 0440 -o root -g root {BOAT_STAGING_DIRECTORY}/sudoers /etc/sudoers.d/boat",
		),
		(
			"installing the boat binary",
			f"sudo chmod 0755 {BOAT_INCOMING_BINARY} && sudo mv -f {BOAT_INCOMING_BINARY} {BOAT_BINARY}",
		),
		(
			"installing boat.service",
			f"sudo install -m 0644 {BOAT_STAGING_DIRECTORY}/boat.service "
			"/etc/systemd/system/boat.service",
		),
		(
			"installing boat-networkd.service",
			f"sudo install -m 0644 {BOAT_STAGING_DIRECTORY}/boat-networkd.service "
			"/etc/systemd/system/boat-networkd.service",
		),
		("reloading systemd", "sudo systemctl daemon-reload"),
	]
	with ssh_key_file(connection.ssh_private_key) as key_path:
		for description, command in steps:
			server._boat_ssh(connection, key_path, description, command)
		# Give boat its HTTP listener. The shipped unit omits --listen on purpose
		# (it awaits a registration handshake that hands over a tunnel address);
		# until that exists, Atlas — the operator the unit defers to — writes the
		# drop-in so the daemon serves the network API every HTTP host verb needs.
		# Written before the token install; start_boat's restart (after the token
		# lands) is what brings the listener up.
		server._boat_ssh(
			connection,
			key_path,
			"installing the boat listener drop-in",
			f"sudo install -D -m 0644 -o root -g root /dev/stdin {BOAT_DROPIN_PATH}",
			stdin=server._boat_dropin_contents(),
		)
		server._boat_ssh(
			connection,
			key_path,
			"reloading systemd for the boat drop-in",
			"sudo systemctl daemon-reload",
		)
		verify_boat_binary(server, connection, key_path, digest)
		# The bearer token last: it is a per-host secret, minted here and written
		# to /etc/boat/token, not one of the four static artifacts the tar stream
		# carried. start_boat's restart reads it.
		install_boat_token(server, connection, key_path)
		# The datum token bundle + its drop-in, when metrics export is configured. A
		# no-op otherwise, so a fleet without datum installs nothing extra.
		install_datum_tokens(server, connection, key_path)


def verify_boat_binary(server, connection, key_path, digest: str) -> None:
	"""Prove the binary that landed is the one Atlas shipped, and that it runs.

	Two different failures, both silent without this: bytes that changed in
	flight (the digest), and a binary built for another architecture or a
	non-executable file (`boat version`, which is also the only way Atlas can
	learn the version — it cannot run a host's binary locally). The answer lands
	on `observed_boat_version` so the field records what was installed at
	install time rather than only what a later mirror sweep happened to see."""
	landed = server._boat_ssh(
		connection, key_path, "reading the installed digest", f"sha256sum {BOAT_BINARY}"
	)
	if landed.split()[:1] != [digest]:
		frappe.throw(
			f"boat on {server.name} is not the binary Atlas shipped: the host reports "
			f"{landed.split()[0] if landed.split() else '(nothing)'}, Atlas shipped {digest}"
		)
	version = server._boat_ssh(
		connection, key_path, "running boat version", f"{BOAT_BINARY} version"
	).strip()
	if not version:
		frappe.throw(f"`boat version` on {server.name} printed nothing; the binary is not usable")
	server.observed_boat_version = version


def install_datum_tokens(server, connection, key_path) -> None:
	"""Ship this host's datum token bundle and the drop-in that points boat at datum.
	A no-op when metrics export is not configured (no atlas_datum_url), so a fleet that
	has not turned datum on installs nothing. The bundle is a single fleet token (resource_id="boat")
	with an empty VM map — host and VMs both report under it, told apart by server=/vm= labels; a
	static `atlas_datum_token` from site config is used when present, else a token is minted. The
	secret travels on stdin, never argv."""
	if not frappe.conf.get("atlas_datum_url"):
		return
	from atlas.atlas import datum_token

	payload = datum_token.single_token_file_json()
	server._boat_ssh(
		connection,
		key_path,
		"installing the datum tokens",
		f"sudo install -D -m 0640 -o root -g boat /dev/stdin {DATUM_TOKENS_PATH}",
		stdin=payload,
	)
	# Rewrite the SINGLE boat drop-in (it folds datum flags in alongside --listen),
	# so turning datum on post-bootstrap keeps the network listener.
	server._boat_ssh(
		connection,
		key_path,
		"installing the boat systemd drop-in (with datum)",
		f"sudo install -D -m 0644 -o root -g root /dev/stdin {BOAT_DROPIN_PATH}",
		stdin=server._boat_dropin_contents(),
	)
	server._boat_ssh(connection, key_path, "reloading systemd for datum", "sudo systemctl daemon-reload")
