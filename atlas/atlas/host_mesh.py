"""Controller side of the WireGuard HOST mesh (the private-plane fabric).

The design lives in `llm/references/private-networking-host-mesh.md`. Each *host*
peers with every other Active host over the hosts' public IPv6 endpoints; a guest
sends plain IPv6 to its tap and the host encapsulates. This module computes each
host's desired peer set (every OTHER Active Server) and its `AllowedIPs` (the
`/128` of every VM currently living on that peer, plus that peer's own infra mesh
address), renders the `wg-mesh.conf`, and CONVERGINGLY reconciles each host's live
`wg-mesh` to it over the controller-over-HOST-SSH path.

It mirrors `atlas/atlas/proxy.py` (read-live / byte-compare / push-on-drift) with
TWO load-bearing differences the design calls out:

  1. TRANSPORT IS HOST-SSH, NOT GUEST-SSH (design §3). The mesh is a HOST fabric;
     `wg-mesh` lives in each host's root netns, invisible to every guest. We reuse
     `connection_for_server` / the root-SSH layer, NOT the guest path.

  2. CONVERGING, NOT LOG-AND-SKIP (design §3). The proxy map is an INBOUND table,
     so its reconcile can skip an unreachable proxy. The mesh is the live cross-host
     FORWARDING fabric — a skipped push is a PARTITION, not a stale row. So
     `reconcile_host_mesh` collects per-host push failures and re-raises, and the
     backstop sweep / lock retries, rather than swallowing the failure.

Isolation is NOT enforced here. `AllowedIPs` pins a `/128` to its HOST (free
host-attribution), but tenant isolation + anti-spoof live in the host nftables
rules `vm-network-up.py` installs at the per-VM veth (design §4). This module only
wires the host-to-host cryptokey routing so an `fdaa::` packet can cross hosts.

CUTOVER EXCEPTION (design §7): steady-state reconcile may converge, but a MIGRATION
cutover must be SEQUENCED (remove-from-source THEN add-to-target) under the Server
lock — a converging 2-peer push can momentarily leave a `/128` in BOTH hosts'
`AllowedIPs`, giving ambiguous delivery. `sequenced_migration_cutover` is the one
non-converging path.

The host private key never lands in an argv or the config file body that goes over
SSH stdin only: it is written to `/etc/atlas-host-mesh.key` (0600) via `tee` from
stdin, exactly like proxy.py's cert push. The config file carries only the
`ListenPort` and the peers; the key is referenced by path.
"""

import frappe

from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
from atlas.atlas.networking import (
	WG_HOST_PORT,
	WIREGUARD_MTU,
	derive_client_address,
	derive_host_mesh_address,
	derive_host_wireguard_keypair,
	derive_private_address,
)
from atlas.atlas.ssh import connection_for_server

MESH_DEVICE = "wg-mesh"
MESH_CONFIG_PATH = "/etc/wireguard/wg-mesh.conf"
MESH_KEY_PATH = "/etc/atlas-host-mesh.key"
MESH_ENV_PATH = "/etc/atlas-host-mesh.env"

# Which VM statuses count as "living on a host" for AllowedIPs. A Terminated VM
# has released its /128; a Draft VM was never placed. Everything else (Running,
# Stopped, Provisioning, …) keeps its /128 advertised so a stop/start does not
# churn the mesh — the address is stable, the VM just is not currently up.
_RESIDENT_EXCLUDED_STATUSES = ("Terminated", "Draft")


def reconcile_host_mesh() -> list[str]:
	"""Reconcile every Active host's `wg-mesh` to the current fleet state.

	Returns the names of the hosts that drifted and were re-synced. Called on:
	  (1) a host reaching Active (end of finish_provisioning) — reconciles the WHOLE
	      mesh, since every other host needs the newcomer as a peer;
	  (2) a host archived / Broken — drop it from everyone's peer set;
	  (3) any VM lifecycle event (provision / terminate) — a /128 joins or leaves
	      exactly one host's AllowedIPs, changing every OTHER host's config;
	  (4) the scheduled backstop sweep (hooks.scheduler_events).

	CONVERGING: collects per-host push failures and re-raises at the end, so the job
	is retried rather than leaving a partitioned fabric. Contrast proxy.reconcile_region,
	which returns quietly on per-proxy failure."""
	hosts = _active_hosts()
	residents = _residents_by_host(hosts)
	synced, failures = [], []
	for host in hosts:
		try:
			if _reconcile_one_host(host, hosts, residents):
				synced.append(host["name"])
		except Exception as exception:  # collected, then re-raised (converging)
			failures.append((host["name"], exception))
	if failures:
		detail = "; ".join(f"{name}: {error}" for name, error in failures)
		frappe.throw(f"Host-mesh reconcile incomplete: {detail}")
	return synced


def _active_hosts() -> list[dict]:
	"""Every Active Server in this (single-region) Atlas deployment — the mesh peer
	universe. Each host's endpoint, wg pubkey, and infra mesh address are DERIVED,
	never stored (design §3, §8): the whole desired mesh reconstructs from the Server
	table. A Server with no ipv6_address (not yet finished provisioning) can't be a
	wg endpoint, so it is skipped until it has one. A Fake-provider Server (the test
	seam) has no real host to SSH, so it is skipped too — the reconcile is then a clean
	no-op on a test/Fake fleet, exactly like run_task hands Fake tasks off without SSH."""
	from atlas.atlas.providers.fake_tasks import is_fake_server

	rows = frappe.get_all(
		"Server",
		filters={"status": "Active"},
		fields=["name", "ipv6_address"],
	)
	hosts = []
	for row in rows:
		if not row.ipv6_address or is_fake_server(row.name):
			continue
		_private_key, public_key = derive_host_wireguard_keypair(row.name)
		hosts.append(
			{
				"name": row.name,
				"endpoint": row.ipv6_address,  # Server.ipv6_address = the wg endpoint
				"public_key": public_key,
				"mesh_address": derive_host_mesh_address(row.name),
			}
		)
	return hosts


def _residents_by_host(hosts: list[dict]) -> dict[str, list[str]]:
	"""Map each host -> the /128s living on it right now: every non-terminated VM on
	that server, ALL tenants mixed. This is the AllowedIPs source of truth, computed
	per-/128 from the Virtual Machine rows (the same shape as the Subdomain->proxy-map
	idiom). A dark VM (public_networking=0) has NO public /128 but IS on the mesh, so
	we key residency on `server`, not on ipv6_address, and derive the private address
	from (tenant, name)."""
	names = [host["name"] for host in hosts]
	rows = frappe.get_all(
		"Virtual Machine",
		filters={"server": ["in", names], "status": ["not in", _RESIDENT_EXCLUDED_STATUSES]},
		fields=["name", "server", "tenant"],
	)
	residents: dict[str, list[str]] = {name: [] for name in names}
	for row in rows:
		if not row.tenant:
			# A VM with no tenant has no derivable /48; it cannot be on the private
			# plane. Skip rather than crash the whole fleet reconcile on one bad row.
			continue
		residents[row.server].append(derive_private_address(row.tenant, row.name))
	_add_customer_vpc_clients(names, residents)
	return residents


def _add_customer_vpc_clients(host_names: list[str], residents: dict[str, list[str]]) -> None:
	"""Fold every Active VPN Peer's client /128 into the AllowedIPs of the host
	that runs its GATEWAY VM (spec/26 §3, reference §6.3). A customer's laptop is a
	"dark VM at the customer's premises": for a tenant's VMs to reach it BACK, every
	other host must route the client /128 to the gateway VM's host — exactly how a VM's
	/128 is advertised. This rides the existing converging reconcile_host_mesh delta-push:
	enrolling a peer makes its /128 appear here (so the reconcile advertises it) and
	revoking it (status → Revoked) makes it disappear (so the reconcile withdraws it) —
	the teardown-bug-safe "reconcile on teardown, not only on enroll" the design flags.

	The client /128 lives on exactly one host (the one running its gateway), non-
	overlapping across peers by construction (each client address is unique), so it slots
	into AllowedIPs alongside the VM /128s with no special-casing. A no-op on a fleet with
	no gateway or no Active peers."""
	# The customer gateway (spec/26, Phase 5) is a later feature than the host mesh
	# (Phase 1): a site running the mesh may not have migrated the `VPN Peer`
	# DocType yet. Treat its absence as "no peers" so the Phase-1 reconcile never
	# hard-fails on a fleet without the gateway installed — the same fail-open posture
	# `_active_hosts` takes toward Fake servers.
	if not frappe.db.exists("DocType", "VPN Peer"):
		return
	peers = frappe.get_all(
		"VPN Peer",
		filters={"status": "Active"},
		fields=["name", "tenant", "gateway"],
	)
	if not peers:
		return
	# The gateway VM → its host, so the client /128 lands in that host's AllowedIPs. One
	# lookup per distinct gateway (there is one gateway per region today).
	gateway_hosts = {
		gateway: frappe.db.get_value("Virtual Machine", gateway, "server")
		for gateway in {peer.gateway for peer in peers if peer.gateway}
	}
	for peer in peers:
		host = gateway_hosts.get(peer.gateway)
		if not host or host not in host_names or not peer.tenant:
			# The gateway VM is on a host outside this reconcile's universe (Fake / not
			# Active), or the peer has no tenant/gateway — skip rather than crash.
			continue
		residents[host].append(derive_client_address(peer.tenant, peer.name))


def render_wg_mesh_config(this_host: str, hosts: list[dict], residents: dict[str, list[str]]) -> str:
	"""The `/etc/wireguard/wg-mesh.conf` body for `this_host` — its key reference +
	one [Peer] per OTHER host. Each peer's AllowedIPs is the enumerated set of /128s
	living on that peer PLUS that peer's own infra mesh address /128 (§2.4), so the
	host↔host bus (NBD, snapshot, image fan-out) can dial the peer over the tunnel.
	Every /128 lives on exactly one host, so the sets are non-overlapping across peers
	by construction — longest-prefix match resolves to exactly one peer. No swap, no
	eBPF, no host bits in the address.

	Canonical, deterministic bytes (peers sorted by pubkey, /128s sorted) so the
	reconcile "in sync?" check is a plain string compare, like proxy.canonical_json.
	The PrivateKey is referenced by PATH, not inlined — the secret never rides in this
	body (which goes over SSH and lands 0600 on disk); the host's own key file
	(MESH_KEY_PATH) carries it. `wg syncconf` reads the key from the running device,
	so the pushed config only needs PostUp-free peer state."""
	lines = [
		"[Interface]",
		f"ListenPort = {WG_HOST_PORT}",
		"",
	]
	for peer in sorted(hosts, key=lambda host: host["public_key"]):
		if peer["name"] == this_host:
			continue
		allowed = sorted(
			[f"{address}/128" for address in residents.get(peer["name"], [])]
			+ [f"{peer['mesh_address']}/128"]
		)
		lines += [
			"[Peer]",
			f"PublicKey = {peer['public_key']}",
			# AllowedIPs is EVERY /128 on the peer host (all tenants) + the peer's own
			# infra mesh /128. Never empty (the mesh address is always present), so a
			# host with no VMs is still a reachable bus endpoint.
			f"AllowedIPs = {', '.join(allowed)}",
			f"Endpoint = [{peer['endpoint']}]:{WG_HOST_PORT}",
			"PersistentKeepalive = 25",  # both ends routed v6, but keepalive is cheap insurance
			"",
		]
	return "\n".join(lines) + "\n"


def _reconcile_one_host(host: dict, hosts: list[dict], residents: dict[str, list[str]]) -> bool:
	"""Read `host`'s live `wg-mesh`, compare to desired, push on drift. Returns True
	iff a push was needed. Raises on a failed push (converging — see module docstring).
	Transport is the controller-over-HOST-SSH path (connection_for_server + run_ssh)."""
	desired = render_wg_mesh_config(host["name"], hosts, residents)
	server = frappe.get_doc("Server", host["name"])
	connection = connection_for_server(server)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		live = _read_live_wg_mesh(connection, key_path, host, hosts, residents)
		# Drift is either a peer/config difference OR a missing interface mesh address:
		# the peer dump (`wg show dump`) does not carry the interface's own /128, so a
		# device whose peers are correct but whose mesh address was never assigned (an
		# interrupted create, a pre-fix device) would otherwise read as "in sync" and never
		# self-heal. Check the address explicitly so the address self-heals like the peers.
		if live == desired and _mesh_address_present(connection, key_path, host["mesh_address"]):
			return False
		_push_wg_mesh(connection, key_path, host, desired)
	return True


def _mesh_address_present(connection, key_path, mesh_address: str) -> bool:
	"""True iff the host's own infra mesh /128 is assigned to the wg-mesh device. Read
	separately from `wg show dump` (which carries only peers), so a device missing its
	own address is treated as drift and re-pushed by the converging reconcile."""
	stdout, _stderr, code = run_ssh(
		connection, key_path, f"ip -6 addr show dev {MESH_DEVICE}", timeout_seconds=30
	)
	return code == 0 and mesh_address in stdout


def _read_live_wg_mesh(
	connection, key_path, host: dict, hosts: list[dict], residents: dict[str, list[str]]
) -> str:
	"""SSH the HOST (root layer), read `wg show wg-mesh dump`, and re-render it into
	`render_wg_mesh_config`'s byte shape so the compare is a plain string equality —
	the proxy.py `canonical_json` idiom.

	`wg show <dev> dump` is tab-separated: the first line is the interface
	(private-key, public-key, listen-port, fwmark); each later line is a peer
	(public-key, preshared-key, endpoint, allowed-ips, latest-handshake, rx, tx,
	keepalive). We reconstruct the PEER stanzas from the live dump and pair each live
	peer's pubkey back to its desired endpoint/mesh identity, so a drift in AllowedIPs
	OR in peer membership shows up as a byte difference. If the device does not exist
	yet (fresh host, dump fails), we return "" so the first reconcile always pushes."""
	stdout, _stderr, code = run_ssh(
		connection, key_path, f"sudo wg show {MESH_DEVICE} dump", timeout_seconds=60
	)
	if code != 0:
		# No device yet (host-mesh.service not up / not reconciled). Force a push.
		return ""
	live_peers: dict[str, list[str]] = {}
	lines = stdout.rstrip("\n").split("\n")
	# Line 0 is the interface. Peer lines follow.
	for raw in lines[1:]:
		if not raw.strip():
			continue
		fields = raw.split("\t")
		public_key = fields[0]
		allowed = fields[3] if len(fields) > 3 else ""
		live_peers[public_key] = sorted(
			part.strip() for part in allowed.split(",") if part.strip() and part.strip() != "(none)"
		)
	# Re-render the SAME canonical shape from the live peer set, keyed on pubkey, so
	# any drift (peer added/removed, AllowedIPs changed) becomes a byte difference
	# against render_wg_mesh_config's output for this host.
	by_pubkey = {peer["public_key"]: peer for peer in hosts}
	rendered = ["[Interface]", f"ListenPort = {_live_listen_port(lines)}", ""]
	for public_key in sorted(live_peers):
		peer = by_pubkey.get(public_key)
		endpoint = f"[{peer['endpoint']}]:{WG_HOST_PORT}" if peer else "?"
		rendered += [
			"[Peer]",
			f"PublicKey = {public_key}",
			f"AllowedIPs = {', '.join(live_peers[public_key])}",
			f"Endpoint = {endpoint}",
			"PersistentKeepalive = 25",
			"",
		]
	_ = residents  # residents feed the DESIRED side (the caller); live is read here
	return "\n".join(rendered) + "\n"


def _live_listen_port(dump_lines: list[str]) -> str:
	"""The listen-port from a `wg show dump` interface line (field 3), or the default
	if the dump is empty/unparseable — so the rendered live-config's [Interface] line
	matches the desired one when in sync."""
	if dump_lines:
		fields = dump_lines[0].split("\t")
		if len(fields) >= 3 and fields[2].isdigit():
			return fields[2]
	return str(WG_HOST_PORT)


def _push_wg_mesh(connection, key_path, host: dict, desired: str) -> None:
	"""Write the desired config to MESH_CONFIG_PATH (0600, via stdin so nothing lands
	in an argv), ensure this host's derived key + env are in place, and `wg syncconf`
	the running device to the new peer set. Raises on any non-zero exit (converging).

	`wg syncconf` applies ONLY the delta between the running config and the file — it
	does not tear peers down and rebuild, so an in-flight tunnel to an unchanged peer
	is undisturbed. The device itself is created by host-mesh.service; if it is not up
	yet we bring it up first from the just-written key/config (self-healing, matching
	the service's own ExecStart)."""
	private_key, _public_key = derive_host_wireguard_keypair(host["name"])
	# 1. This host's derived private key (0600) — the secret, via stdin only.
	_write_host_file(connection, key_path, MESH_KEY_PATH, private_key + "\n", mode="0600")
	# 2. The env file the host-mesh.service reads (ConditionPathExists gate).
	_write_host_file(connection, key_path, MESH_ENV_PATH, _mesh_env_body(host), mode="0644")
	# 3. The peer config (0600 — no secret in it, but WireGuard configs are 0600 by
	#    convention and `wg-quick strip` warns otherwise).
	_write_host_file(connection, key_path, MESH_CONFIG_PATH, desired, mode="0600")
	# 4. Ensure the device exists and carries this host's key, then syncconf the peers.
	#    `wg syncconf <dev> <(wg-quick strip <conf>)` is the idempotent apply; we do the
	#    strip on-host so a PostUp-free file is fed to syncconf. If the device is absent
	#    we create+key+route it first (the service's ExecStart, inlined for self-heal).
	#    Runs the whole thing under `bash -c` (one auto-quoted param) because process
	#    substitution needs bash and the remote login shell is not guaranteed to be it.
	stdout, stderr, code = run_ssh(
		connection, key_path, "sudo bash -c {}", _apply_script(host["mesh_address"]), timeout_seconds=120
	)
	if code != 0:
		frappe.throw(f"wg syncconf to host {host['name']} failed (exit {code}): {stderr[-500:]}")
	_ = stdout


def _apply_script(mesh_address: str) -> str:
	"""The on-host bring-up-or-sync script body (fed to `bash -c` as one argv token).
	Idempotent: create `wg-mesh` if missing, then ALWAYS (create-or-replace) pin MTU,
	bring it up, add the fdaa::/16 route, and assign this host's OWN infra mesh /128
	(§2.4 — the host↔host bus endpoint); then `wg syncconf` the peer set from the pushed
	file, and — LAST — set the derived private key + listen port.

	The MTU/route/address are asserted UNCONDITIONALLY (not only on first create), so a
	device left half-configured by an interrupted prior run self-heals on the next push —
	`ip ... replace` / `ip link set` are idempotent. Assigning the mesh address here (not
	only in host-mesh.service's boot path) is load-bearing: the controller push is what
	makes the host reachable on the bus without waiting for a reboot.

	The key MUST be set AFTER syncconf, not before: `wg syncconf` applies the WHOLE
	[Interface] section from the file, and the pushed config deliberately carries NO
	PrivateKey (the secret rides in its own 0600 key file, never in the config body).
	Verified on a real Scaleway host: `wg set private-key` THEN `wg syncconf` leaves
	the interface key as `(none)` (syncconf clears the unmentioned key), whereas
	`syncconf` THEN `wg set private-key` preserves the key, peers, and listen-port.
	So the order below is load-bearing, not incidental."""
	return (
		f"set -e; "
		f"if ! ip link show {MESH_DEVICE} >/dev/null 2>&1; then "
		f"ip link add dev {MESH_DEVICE} type wireguard; "
		f"fi; "
		# Always (re-)assert MTU, the host's own mesh /128, up, and the route — idempotent
		# create-or-replace so a device that exists but lacks the address self-heals.
		f"ip link set dev {MESH_DEVICE} mtu {WIREGUARD_MTU}; "
		f"ip -6 addr replace {mesh_address}/128 dev {MESH_DEVICE}; "
		f"ip link set dev {MESH_DEVICE} up; "
		f"ip -6 route replace fdaa::/16 dev {MESH_DEVICE}; "
		# syncconf the peers from the stripped file FIRST (it rewrites [Interface],
		# clearing any unmentioned private key), THEN (re-)assert the derived key +
		# listen-port so they survive. Order is load-bearing — see the docstring.
		f"wg syncconf {MESH_DEVICE} <(wg-quick strip {MESH_CONFIG_PATH}); "
		f"wg set {MESH_DEVICE} private-key {MESH_KEY_PATH} listen-port {WG_HOST_PORT}"
	)


def _mesh_env_body(host: dict) -> str:
	"""The `/etc/atlas-host-mesh.env` body host-mesh.service reads. Carries the host's
	own mesh address (§2.4) so the service can assign it to the device on boot, plus
	the device/port constants — no secret (the key is its own 0600 file)."""
	return (
		f"MESH_DEVICE={MESH_DEVICE}\n"
		f"MESH_ADDRESS={host['mesh_address']}\n"
		f"WG_HOST_PORT={WG_HOST_PORT}\n"
		f"WIREGUARD_MTU={WIREGUARD_MTU}\n"
	)


def _write_host_file(connection, key_path, path: str, content: str, *, mode: str) -> None:
	"""Write `content` to `path` on the HOST via `tee` (content arrives on stdin, so a
	secret never lands in an argv), creating the parent dir and chmod-ing. Mirrors
	proxy._write_guest_file but over the host-SSH connection. Raises on failure."""
	parent = path.rsplit("/", 1)[0] or "/"
	_stdout, stderr, code = run_ssh(
		connection,
		key_path,
		f"sudo install -d -m 0755 {{}} && sudo tee {{}} >/dev/null && sudo chmod {mode} {{}}",
		parent,
		path,
		path,
		timeout_seconds=60,
		stdin=content,
	)
	if code != 0:
		frappe.throw(f"Writing {path} to host failed (exit {code}): {stderr[-300:]}")


def sequenced_migration_cutover(vm_name: str, source_host: str, target_host: str) -> None:
	"""The ONE non-converging path (design §7). WireGuard requires non-overlapping
	AllowedIPs across peers; during a migration the VM's /128 must never be in BOTH the
	source and target hosts' AllowedIPs at once (ambiguous delivery). So the cutover is
	ORDERED — remove-from-source, THEN add-to-target — under the Server lock spec/09
	already calls for. The address itself never changes (host-independent), only which
	peer advertises it. Steady-state reconcile may converge; this must sequence.

	The caller holds the Server lock and has ALREADY repointed the VM's `server` in the
	DB to the target (the migration cutover). We recompute residency from the DB but
	force the ordering: first push a fleet config with the VM dropped from BOTH hosts,
	then a second push with it present on the target only."""
	hosts = _active_hosts()
	residents = _residents_by_host(hosts)
	tenant = frappe.db.get_value("Virtual Machine", vm_name, "tenant")
	private_address = derive_private_address(tenant, vm_name)

	# 1. Withdraw the /128 from BOTH source and target, push EVERY peer, so no host
	#    routes it anywhere transiently. (residents already reflects the DB re-home, so
	#    the VM is on the target; drop it from both to reach the empty intermediate.)
	for name in (source_host, target_host):
		residents.setdefault(name, [])
		residents[name] = [address for address in residents[name] if address != private_address]
	_push_fleet(hosts, residents)

	# 2. THEN add it under the target only and push again — now unambiguous.
	residents.setdefault(target_host, [])
	if private_address not in residents[target_host]:
		residents[target_host].append(private_address)
	_push_fleet(hosts, residents)


def _push_fleet(hosts: list[dict], residents: dict[str, list[str]]) -> None:
	"""Push a given residents map to EVERY host, raising on the first failure (the
	sequenced cutover cannot tolerate a partial fleet — an un-pushed host would keep
	the stale AllowedIPs). Used only by the sequenced cutover."""
	for host in hosts:
		server = frappe.get_doc("Server", host["name"])
		connection = connection_for_server(server)
		desired = render_wg_mesh_config(host["name"], hosts, residents)
		with ssh_key_file(connection.ssh_private_key) as key_path:
			_push_wg_mesh(connection, key_path, host, desired)


def reconcile_all_host_meshes() -> None:
	"""Idempotent backstop (hooks.scheduler_events): re-reconcile the whole host mesh
	so a rebooted/rebuilt/drifted host self-heals without operator action. The
	forwarding fabric's converging-reconcile guarantee (design §3) — without this, a
	missed push is a permanent partition. Logs failure but is safe to re-run each tick."""
	try:
		reconcile_host_mesh()
	except Exception as exception:
		frappe.log_error(f"Host-mesh sweep failed: {exception}", "Host mesh sweep")


def enqueue_reconcile_host_mesh() -> None:
	"""Enqueue a background reconcile from a lifecycle event (a host reaching Active, a
	VM provision/terminate). Enqueued — never run inline — so a mesh push failure never
	rolls back the lifecycle transaction that triggered it: the row commits, and the
	converging reconcile (retried by the job, backstopped by the scheduler sweep) brings
	the fabric to match. `enqueue_after_commit` so the worker reads the just-committed
	state; a fixed `job_id` coalesces a burst of events (e.g. terminate fan-out) into one
	pending job — a full reconcile already reflects every committed change.

	A no-op when there is no worker/queue context (a bare `bench execute` in a test): the
	reconcile is idempotent and the scheduler sweep will pick up the drift, so swallowing
	the enqueue failure keeps the lifecycle path clean on a site without a running worker."""
	try:
		frappe.enqueue(
			"atlas.atlas.host_mesh.reconcile_all_host_meshes",
			queue="long",
			timeout=600,
			enqueue_after_commit=True,
			job_id="reconcile_host_mesh",
			deduplicate=True,
		)
	except Exception as exception:
		frappe.logger("atlas").warning(f"host-mesh reconcile enqueue skipped: {exception}")
