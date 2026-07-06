"""Controller side of the customer gateway (spec/25 Phase 5, spec/26).

A customer runs stock `wg-quick`, dials the region's GATEWAY VM's fixed public v4
`:51820`, and lands inside their tenant's /48 — reaching every VM in their VPC by
its stable `fdaa:` address. This module is the gateway VM's control plane, the
sibling of `atlas/atlas/proxy.py`:

  - `reconcile_gateway(gateway_vm)` renders the desired `[Peer]` set from the Active
    `VPN Peer` rows and `wg syncconf`s the gateway's single `wg0` over
    GUEST-SSH (the proxy idiom — the gateway is a guest, NOT the host-SSH mesh path),
    convergent + idempotent like `reconcile_proxy`.
  - `request_vpc_access` / `enroll_peer` / `revoke_peer` are the hot-path drivers a
    `VPN Peer` row calls: reconcile the gateway's wg0, then trigger a host-
    mesh reconcile so the client /128 is advertised (return path) / withdrawn.

TWO ISOLATION MECHANISMS, neither needing per-customer state (reference §6):
  1. SOURCE is pinned by WireGuard itself — each peer's host-side AllowedIPs is that
     customer's own client /128, so cryptokey routing drops (in-kernel, pre-nft) any
     packet whose inner source isn't that exact /128. A customer can't forge another
     tenant's source even sharing wg0 with thousands of others.
  2. DESTINATION is confined to the source's own /48 by a STATIC `same_48` eBPF guard
     baked onto the gateway's wg0 (host-side, `gateway-up.py`) — one program for ALL
     customers, never touched per enroll. Since the source is pinned to the client's
     own tenant, "same /48 as source" ≡ "the client's own VPC."

So enrolling a customer is JUST adding the wg peer with its /128 source pin — no new
interface, no new nft rule, no eBPF change. The gateway's shared `wg0` public key is
read from the gateway (one key per gateway) and denormed onto the row for the config
dialog; it is not per-row identity.
"""

import frappe

from atlas.atlas import scripts_catalog
from atlas.atlas._ssh._quote import substitute
from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
from atlas.atlas.networking import WG_GATEWAY_PORT, WIREGUARD_MTU
from atlas.atlas.ssh import connection_for_guest

# The gateway's single WireGuard interface. One per gateway VM, shared by every
# customer peer — the whole "peers, not interfaces" fix (reference §2).
GATEWAY_DEVICE = "wg0"
GATEWAY_CONFIG_PATH = "/etc/wireguard/wg0.conf"
GATEWAY_KEY_PATH = "/etc/wireguard/wg0.key"  # the gateway's minted 0600 wg0 key (see gateway.py)
GATEWAY_ENV_PATH = "/etc/atlas-gateway.env"

# Where the minimal atlas package + the compiled eBPF guard land INSIDE the gateway
# guest, so gateway.service can `import atlas.gateway`. Mirrors the host's durable
# /var/lib/atlas/bin layout, but staged into the guest by deploy_gateway (the gateway
# is a guest, not a host that bootstrap touches).
GUEST_BIN = "/var/lib/atlas/bin"
GUEST_PACKAGE = f"{GUEST_BIN}/atlas"
# The atlas package modules gateway.py imports (kept minimal — no frappe in the guest).
_GATEWAY_PACKAGE_MODULES = ("__init__.py", "_run.py", "network_env.py", "gateway.py")


def resolve_region_gateway() -> str:
	"""The region's one gateway VM (the `is_gateway=1`, non-Terminated Virtual Machine).

	A single-region deployment runs exactly one gateway (spec/26 §2). Fails loud if
	none exists ("stand up a gateway VM first") or — defensively — if more than one is
	live (a second gateway is a deliberate shard that must record which peers it carries;
	until that path exists, ambiguity is an error, not a silent pick)."""
	gateways = frappe.get_all(
		"Virtual Machine",
		filters={"is_gateway": 1, "status": ["!=", "Terminated"]},
		pluck="name",
	)
	if not gateways:
		frappe.throw("No customer gateway VM is provisioned for this region (set is_gateway on one)")
	if len(gateways) > 1:
		frappe.throw(
			f"More than one active gateway VM found ({', '.join(gateways)}); expected one per region"
		)
	return gateways[0]


@frappe.whitelist()
def deploy_gateway(gateway_vm: str) -> bool:
	"""Stand the gateway's wg0 + static same_48 guard up INSIDE the gateway guest, over
	guest-SSH (the proxy idiom). Idempotent — safe to re-run after a reboot or a rebuild.

	Stages a minimal atlas package (gateway.py + its host-lib deps, no frappe) under the
	guest's /var/lib/atlas/bin, ships the eBPF SOURCE and compiles it to vpc_guard.bpf.o
	ON THE GUEST (clang/libbpf are present on the gateway image; the runtime needs only
	the .o), writes /etc/atlas-gateway.env (port + MTU), installs + enables gateway.service,
	and runs bring_up_gateway once so the interface is live now (not only on next boot).

	Returns True. Raises on any host failure (a gateway without its guard must not run)."""
	vm = frappe.get_doc("Virtual Machine", gateway_vm)
	if not vm.is_gateway:
		frappe.throw(f"{gateway_vm} is not a customer gateway (is_gateway unset)")
	directory = scripts_catalog.scripts_directory()
	package_dir = directory / "lib" / "atlas"
	connection = connection_for_guest(vm)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		# 1. The minimal atlas package modules gateway.py needs (pure code, no frappe).
		run_ssh(connection, key_path, f"sudo install -d -m 0755 {GUEST_PACKAGE}", timeout_seconds=30)
		for module in _GATEWAY_PACKAGE_MODULES:
			_stage_guest_file(
				connection, key_path, (package_dir / module).read_text(), f"{GUEST_PACKAGE}/{module}", "0644"
			)
		# 2. The eBPF SOURCE, compiled to a .o on the guest (clang is build-time only;
		#    the .o is what bring_up_gateway attaches). The .c is host-verified verbatim.
		_stage_guest_file(
			connection,
			key_path,
			(directory / "bpf" / "vpc_guard.bpf.c").read_text(),
			f"{GUEST_PACKAGE}/vpc_guard.bpf.c",
			"0644",
		)
		_compile_guard(connection, key_path, gateway_vm)
		# 3. The env file (port + MTU) + the boot-safe service.
		_stage_guest_file(connection, key_path, _gateway_env_body(), GATEWAY_ENV_PATH, "0644")
		_stage_guest_file(
			connection,
			key_path,
			(directory / "systemd" / "gateway.service").read_text(),
			"/etc/systemd/system/gateway.service",
			"0644",
		)
		# 4. Run bring-up NOW, directly (not only via the service), so any error surfaces
		#    here as a failed Task instead of a silent oneshot failure. Then enable the
		#    service for boot persistence. bring_up_gateway runs under the guest's system
		#    python3 (no host venv on a guest); it needs only stdlib + the staged
		#    atlas._run / atlas.network_env.
		bring_up = (
			"sudo /usr/bin/python3 -c "
			"\"import sys; sys.path.insert(0, '/var/lib/atlas/bin'); "
			'from atlas.gateway import bring_up_gateway; bring_up_gateway()"'
		)
		stdout, stderr, code = run_ssh(connection, key_path, bring_up, timeout_seconds=180)
		if code != 0:
			frappe.throw(f"bring_up_gateway on {gateway_vm} failed (exit {code}): {stderr[-500:]}")
		# Enable for boot persistence (the ExecStart re-asserts on reboot; already up now).
		_stdout, enable_err, enable_code = run_ssh(
			connection,
			key_path,
			"sudo systemctl daemon-reload && sudo systemctl enable gateway.service 2>&1",
			timeout_seconds=60,
		)
		if enable_code != 0:
			frappe.throw(
				f"Enabling gateway.service on {gateway_vm} failed (exit {enable_code}): {enable_err[-300:]}"
			)
	# The gateway is a TRANSIT router on its host, unlike a tenant VM — wire the two
	# host-forward rules that let its veth bridge the private plane (over HOST-SSH).
	_wire_gateway_host_forwarding(gateway_vm)
	_record(gateway_vm, "gateway-deploy", stdout, stderr, code)
	return True


def _wire_gateway_host_forwarding(gateway_vm: str) -> None:
	"""Insert the two `inet atlas forward` rules on the gateway VM's HOST that let the
	gateway veth bridge the private plane (over HOST-SSH — the mesh/host layer). A tenant
	VM's veth carries only its own /128; the gateway veth is a TRANSIT for the whole client
	range ↔ every tenant /48 it reaches, so it needs router rules a tenant VM never gets:

	  1. gateway → mesh: `iifname <gw-veth> ip6 daddr fdaa::/16 accept` — a client packet
	     the gateway decrypted and forwarded out its eth0 arrives on <gw-veth>; accept it so
	     the host routes it onward via wg-mesh to the destination VM's host. Trusting the
	     gateway veth for fdaa:: transit is safe: the static same_48 eBPF guard on wg0
	     ALREADY dropped any cross-tenant packet before it left the gateway (reference §6.2),
	     and the gateway is an operator-owned jailed guest bounded like any mesh guest (§6.4).
	  2. mesh → gateway: `iifname wg-mesh oifname <gw-veth> ip6 daddr fdaa::/16 accept` — a
	     VM's reply to a client /128 (which the mesh routes to this host) is delivered into
	     the gateway veth so wg0 can encrypt it back to the customer (the return path).

	Inserted at the head (above the terminal `fdaa::/16 drop`), idempotent via a
	list-and-skip guard. A tenant VM's own per-veth rules are untouched."""
	from atlas.atlas.networking import derive_veth_pair
	from atlas.atlas.ssh import connection_for_server, run_ssh, ssh_key_file

	server = frappe.db.get_value("Virtual Machine", gateway_vm, "server")
	from atlas.atlas.providers.fake_tasks import is_fake_server

	if not server or is_fake_server(server):
		return  # a Fake gateway has no host to wire
	host_veth, _guest_veth = derive_veth_pair(gateway_vm)
	rules = [
		f'iifname "{host_veth}" ip6 daddr fdaa::/16 accept',
		f'iifname "wg-mesh" oifname "{host_veth}" ip6 daddr fdaa::/16 accept',
	]
	connection = connection_for_server(frappe.get_doc("Server", server))
	with ssh_key_file(connection.ssh_private_key) as key_path:
		live, _stderr, _code = run_ssh(
			connection, key_path, "sudo nft list chain inet atlas forward", timeout_seconds=30
		)
		for rule in rules:
			if rule in live:
				continue  # already present — idempotent
			_stdout, stderr, code = run_ssh(
				connection, key_path, f"sudo nft insert rule inet atlas forward {rule}", timeout_seconds=30
			)
			if code != 0:
				frappe.throw(
					f"Wiring gateway host-forward rule on {server} failed (exit {code}): {stderr[-300:]}"
				)


def _compile_guard(connection, key_path, gateway_vm: str) -> None:
	"""Compile vpc_guard.bpf.c → vpc_guard.bpf.o on the guest. clang + the bpf headers are
	build-time only; the runtime attaches only the .o. The two gotchas (wg0 is L3, section
	is `tc`) are baked into the .c, so this is a plain compile. Idempotent — recompiling
	overwrites the .o.

	Ensures the toolchain first: a purpose-baked gateway image ships clang + libbpf-dev, but
	a generic image (the e2e base) may not, so install them if `clang` is absent (a no-op on
	an image that already has them). This keeps the runtime attach dependency-free while
	letting the deploy bootstrap the build deps on any Debian/Ubuntu guest."""
	obj = f"{GUEST_PACKAGE}/vpc_guard.bpf.o"
	source = f"{GUEST_PACKAGE}/vpc_guard.bpf.c"
	ensure_toolchain = (
		"command -v clang >/dev/null 2>&1 || "
		"{ sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
		"clang libbpf-dev linux-libc-dev libc6-dev >/dev/null; }"
	)
	compile_cmd = f"clang -O2 -g -target bpf -c {source} -o {obj} -I/usr/include/$(uname -m)-linux-gnu"
	stdout, stderr, code = run_ssh(
		connection, key_path, "sudo bash -c {}", f"{ensure_toolchain} && {compile_cmd}", timeout_seconds=300
	)
	if code != 0:
		frappe.throw(
			f"Compiling the same_48 eBPF guard on {gateway_vm} failed (exit {code}): {stderr[-500:]}. "
			"The gateway image needs clang + libbpf headers (auto-install failed)."
		)
	_ = stdout


def _gateway_env_body() -> str:
	"""The /etc/atlas-gateway.env body bring_up_gateway reads: the wg0 port + MTU. No
	secret (the wg0 key is minted on the guest into its own 0600 file)."""
	return f"WG_GATEWAY_PORT={WG_GATEWAY_PORT}\nWIREGUARD_MTU={WIREGUARD_MTU}\n"


def _stage_guest_file(connection, key_path, content: str, path: str, mode: str) -> None:
	"""Write `content` to `path` in the guest via `tee` (content on stdin, never in argv),
	creating the parent dir + chmod-ing. The proxy._write_guest_file idiom over the gateway
	guest connection. Raises on failure."""
	parent = path.rsplit("/", 1)[0] or "/"
	command = substitute(
		f"sudo install -d -m 0755 {{}} && sudo tee {{}} >/dev/null && sudo chmod {mode} {{}}",
		(parent, path, path),
	)
	_stdout, stderr, code = run_ssh(connection, key_path, command, timeout_seconds=60, stdin=content)
	if code != 0:
		frappe.throw(f"Staging {path} to gateway failed (exit {code}): {stderr[-300:]}")


def _record(gateway_vm: str, script: str, stdout: str, stderr: str, code: int) -> None:
	"""Record a gateway guest op as a Task row for the audit trail (the proxy idiom)."""
	from atlas.atlas.proxy import _record_guest_task

	_record_guest_task(gateway_vm, script, {}, stdout, stderr, code)


@frappe.whitelist()
def request_vpc_access(tenant: str, client_public_key: str, label: str) -> dict:
	"""The single entry point a customer's action funnels through (whitelisted, owner-
	scoped + Central-callable as the service user, spec/16).

	Validate the key, resolve the region gateway, insert the `VPN Peer` row,
	reconcile the gateway's wg0 so it carries the peer, advertise the client /128 into
	the mesh (return path), and return the copy-paste config. The row's before_insert
	validates the key + resolves the gateway; enroll_peer does the host work and flips
	the row Active only once the gateway actually carries the peer."""
	peer = frappe.get_doc(
		{
			"doctype": "VPN Peer",
			"tenant": tenant,
			"label": label,
			"client_public_key": client_public_key,
		}
	)
	peer.insert()
	enroll_peer(peer)
	return client_config_payload(peer)


def enroll_peer(peer) -> None:
	"""Apply `peer` on its gateway's wg0 and advertise its /128 into the mesh. Idempotent —
	the reconcile computes the FULL desired peer set, so re-running is safe (a re-enroll
	after a gateway rebuild).

	Mark Active FIRST, then reconcile: render_wg0_config renders only Active peers, so the
	peer must be Active for this reconcile to push it onto wg0. reconcile_gateway raises on
	a gateway it can't reach, which rolls back the Active db_set within this request
	transaction — so the row only stays Active if the gateway actually carries the peer.
	The mesh reconcile (which also folds only Active peers) then advertises the client /128
	so the tenant's VMs can reach the laptop back (reference §5.2/§6.3)."""
	if peer.status != "Active":
		peer.db_set("status", "Active")
	reconcile_gateway(peer.gateway)
	_reconcile_host_mesh_after_commit()


def revoke_peer(peer) -> None:
	"""Drop `peer` from its gateway's wg0 and withdraw its /128 from the mesh, then mark
	Revoked. Order matters: mark Revoked FIRST so the gateway reconcile (which renders
	only non-Revoked peers) drops it, then the mesh reconcile (which folds only Active
	peers) withdraws its /128. A gateway that is gone (Terminated) needs no wg0 push —
	its peers are already gone — mirroring Reserved IP.detach skipping a dead VM."""
	peer.db_set("status", "Revoked")
	gateway_alive = frappe.db.get_value("Virtual Machine", peer.gateway, "status") != "Terminated"
	if gateway_alive:
		reconcile_gateway(peer.gateway)
	_reconcile_host_mesh_after_commit()


def _reconcile_host_mesh_after_commit() -> None:
	"""Enqueue a converging host-mesh reconcile so the client /128 is advertised /
	withdrawn fleet-wide (rides the existing delta-push, reference §6.3). Enqueued
	after-commit + deduped, exactly like the VM lifecycle triggers — a push failure
	never rolls back the peer transaction; the converging reconcile + scheduler sweep
	bring the fabric to match."""
	from atlas.atlas.host_mesh import enqueue_reconcile_host_mesh

	enqueue_reconcile_host_mesh()


def reconcile_gateway(gateway_vm: str) -> bool:
	"""Reconcile one gateway VM's wg0 to the desired peer set. Returns True iff a sync was
	needed (the live peers drifted from the rows). Convergent + idempotent, the proxy
	idiom: read `wg show wg0 dump`, byte-compare against the desired canonical config,
	`wg syncconf` on drift. A single enroll/revoke is the hot-path delta; a full sweep
	(e.g. after a gateway rebuild) re-pushes every Active peer.

	Reads the gateway's own wg0 public key back and denorms it onto every Active peer row
	whose server_public_key is stale, so the config dialog renders without re-SSHing."""
	vm = frappe.get_doc("Virtual Machine", gateway_vm)
	if not vm.is_gateway:
		frappe.throw(f"{gateway_vm} is not a customer gateway (is_gateway unset)")
	desired = render_wg0_config(gateway_vm)
	connection = connection_for_guest(vm)
	drifted = False
	with ssh_key_file(connection.ssh_private_key) as key_path:
		server_public_key = _read_gateway_public_key(connection, key_path, gateway_vm)
		live = _read_live_wg0(connection, key_path, gateway_vm)
		if live != desired:
			_push_wg0(connection, key_path, gateway_vm, desired)
			drifted = True
		# Route each client /128 INTO wg0 in the guest. `wg set` (unlike `wg-quick`) does NOT
		# install a route for a peer's AllowedIPs, so without this the gateway would send a
		# client-destined reply back out eth0 (its fdaa::/16 default) — a routing loop the
		# host bounces as "time exceeded." A per-client `dev wg0` route sends replies into
		# wg0 to be encrypted back to the customer. Reconciled from the rows (add Active,
		# remove Revoked), the same guest-SSH session as the wg0 push.
		_reconcile_guest_client_routes(connection, key_path, gateway_vm)
	_denormalize_server_public_key(gateway_vm, server_public_key)
	# Route each Active client /128 to the gateway VM's veth ON ITS HOST, so a VM's reply
	# to the client (which the mesh routes to this host) is delivered into the gateway (not
	# black-holed out the blanket `fdaa::/16 dev wg-mesh` route). Reconciled from the rows.
	_reconcile_gateway_host_routes(gateway_vm)
	return drifted


def _reconcile_guest_client_routes(connection, key_path, gateway_vm: str) -> None:
	"""Install/withdraw a `<client>/128 dev wg0` route IN THE GATEWAY GUEST for every
	Active/Revoked peer. `wg set` does not add AllowedIPs routes (only `wg-quick` does), so
	this is what sends a client-destined packet into wg0 (to be encrypted back to the
	customer) instead of out the guest's `fdaa::/16 via fe80::1` default — the loop that
	otherwise bounces the VM's reply as ICMP time-exceeded. Idempotent (`route replace` for
	Active, `route del` tolerating absence for Revoked)."""
	peers = frappe.get_all(
		"VPN Peer",
		filters={"gateway": gateway_vm, "status": ["in", ("Active", "Revoked")]},
		fields=["client_address", "status"],
	)
	for peer in peers:
		if peer.status == "Active":
			command = f"ip -6 route replace {peer.client_address}/128 dev {GATEWAY_DEVICE}"
		else:
			command = f"ip -6 route del {peer.client_address}/128 dev {GATEWAY_DEVICE} 2>/dev/null || true"
		_stdout, stderr, code = run_ssh(connection, key_path, "sudo bash -c {}", command, timeout_seconds=30)
		if code != 0 and peer.status == "Active":
			frappe.throw(
				f"Routing client {peer.client_address} into wg0 on {gateway_vm} failed: {stderr[-200:]}"
			)


def _reconcile_gateway_host_routes(gateway_vm: str) -> None:
	"""Wire the client-return path on the gateway VM's HOST for every Active/Revoked peer
	(over HOST-SSH — the host owns routing). A VM's reply to a client (`dst=<client>/128`)
	must reach the gateway GUEST's eth0 so the guest routes it into wg0; that takes TWO
	routes, mirroring how vm-network-up.py routes a VM's OWN /128:

	  1. ROOT netns: `<client>/128 via fe80::3 dev <gw-veth>` — more specific than the
	     blanket `fdaa::/16 dev wg-mesh`, so the reply is sent into the gateway's netns
	     (fe80::3 is the veth namespace-side link-local, fixed by vm-network-up.py) instead
	     of black-holed out the mesh.
	  2. GATEWAY NETNS: `<client>/128 via <guest-link-local> dev <tap>` — inside the netns,
	     route the client to the GUEST's own eth0 link-local over the tap. The `via` is
	     load-bearing: the client /128 is a FORWARDED address the guest does not own, so a
	     plain `dev <tap>` route has no ND neighbor to resolve and the netns's
	     `default via fe80::2` bounces the reply back to the host — an hlim-decrementing loop
	     that dies as ICMP time-exceeded (confirmed on a real host). Routing `via` the guest's
	     own link-local (which it answers ND for) delivers it to the guest, which then
	     forwards eth0 → wg0. The link-local is EUI-64-derived from the VM MAC (no probing).

	Reconciled from the rows: Active peers get both routes (idempotent `ip route replace`),
	Revoked peers have them removed — the teardown-safe symmetry the mesh /128 push has."""
	from atlas.atlas.networking import derive_guest_link_local, derive_netns, derive_tap, derive_veth_pair
	from atlas.atlas.providers.fake_tasks import is_fake_server
	from atlas.atlas.ssh import connection_for_server, run_ssh, ssh_key_file

	server = frappe.db.get_value("Virtual Machine", gateway_vm, "server")
	if not server or is_fake_server(server):
		return
	host_veth, _guest_veth = derive_veth_pair(gateway_vm)
	netns = derive_netns(gateway_vm)
	tap = derive_tap(gateway_vm)
	guest_link_local = derive_guest_link_local(gateway_vm)
	peers = frappe.get_all(
		"VPN Peer",
		filters={"gateway": gateway_vm, "status": ["in", ("Active", "Revoked")]},
		fields=["client_address", "status"],
	)
	connection = connection_for_server(frappe.get_doc("Server", server))
	with ssh_key_file(connection.ssh_private_key) as key_path:
		for peer in peers:
			client = f"{peer.client_address}/128"
			if peer.status == "Active":
				commands = [
					f"ip -6 route replace {client} via fe80::3 dev {host_veth}",  # root netns → gateway netns
					# netns → guest, via the guest's own link-local (it answers ND; a bare
					# `dev tap` has no neighbor for a forwarded /128 and loops).
					f"ip netns exec {netns} ip -6 route replace {client} via {guest_link_local} dev {tap}",
				]
			else:
				commands = [
					f"ip -6 route del {client} dev {host_veth} 2>/dev/null || true",
					f"ip netns exec {netns} ip -6 route del {client} dev {tap} 2>/dev/null || true",
				]
			for command in commands:
				_stdout, stderr, code = run_ssh(
					connection, key_path, "sudo bash -c {}", command, timeout_seconds=30
				)
				if code != 0 and peer.status == "Active":
					frappe.throw(f"Wiring client return route {client} on {server} failed: {stderr[-200:]}")


def render_wg0_config(gateway_vm: str) -> str:
	"""The desired `wg0.conf` peer body: one `[Peer]` per Active `VPN Peer` on
	this gateway, each pinned to its own client /128 (the source pin, reference §6.1).

	Canonical, deterministic bytes (peers sorted by public key) so the reconcile "in
	sync?" check is a plain string compare, the proxy.canonical_json discipline. Carries
	NO `[Interface]` key/port — those are baked on the gateway and never rewritten by a
	peer sync (the wg-mesh key-vs-syncconf lesson); `wg syncconf` from this peer-only file
	applies just the peer delta. A gateway with no Active peers renders an empty body,
	which `wg syncconf` reads as "no peers" — correctly draining a fully-revoked gateway."""
	peers = frappe.get_all(
		"VPN Peer",
		filters={"gateway": gateway_vm, "status": "Active"},
		fields=["client_public_key", "client_address"],
	)
	stanzas = []
	for peer in sorted(peers, key=lambda row: row.client_public_key or ""):
		# AllowedIPs is the client's OWN /128 — the source pin. WireGuard drops any packet
		# from this peer whose inner source isn't this exact address (reference §6.1).
		stanzas.append(
			f"[Peer]\nPublicKey = {peer.client_public_key}\nAllowedIPs = {peer.client_address}/128\n"
		)
	return "\n".join(stanzas) + ("\n" if stanzas else "")


def _read_live_wg0(connection, key_path, gateway_vm: str) -> str:
	"""SSH the gateway guest, read `wg show wg0 dump`, and re-render it into
	render_wg0_config's byte shape so the compare is a plain string equality (the
	proxy.canonical_json idiom). If the device doesn't exist yet (fresh gateway, dump
	fails) return "" so the first reconcile always pushes.

	`wg show <dev> dump` is tab-separated: line 0 is the interface, each later line a peer
	(public-key, preshared-key, endpoint, allowed-ips, …). We reconstruct the peer stanzas
	from the live dump; a customer peer's AllowedIPs is a single /128, so we keep it
	verbatim, sorted like the desired render."""
	stdout, _stderr, code = run_ssh(
		connection, key_path, f"sudo wg show {GATEWAY_DEVICE} dump", timeout_seconds=60
	)
	if code != 0:
		return ""
	live: dict[str, str] = {}
	lines = stdout.rstrip("\n").split("\n")
	for raw in lines[1:]:  # line 0 is the interface
		if not raw.strip():
			continue
		fields = raw.split("\t")
		public_key = fields[0]
		allowed = fields[3] if len(fields) > 3 else ""
		# A customer peer has exactly one AllowedIP (its /128). Keep the first; "(none)"
		# means a peer with no allowed-ips, which we render as an empty AllowedIPs.
		first = next(
			(part.strip() for part in allowed.split(",") if part.strip() and part.strip() != "(none)"),
			"",
		)
		live[public_key] = first
	stanzas = []
	for public_key in sorted(live):
		stanzas.append(f"[Peer]\nPublicKey = {public_key}\nAllowedIPs = {live[public_key]}\n")
	return "\n".join(stanzas) + ("\n" if stanzas else "")


def _push_wg0(connection, key_path, gateway_vm: str, desired: str) -> None:
	"""Write the desired peer config (0600, via stdin so nothing lands in an argv) and
	`wg syncconf` the running wg0 to it, THEN re-assert the interface key + listen port.
	Raises on any non-zero exit.

	`wg syncconf` applies the peer delta (an in-flight tunnel to an unchanged peer is
	undisturbed), but it ALSO rewrites the WHOLE `[Interface]` from the file — and this
	file is peer-only (no `[Interface]`), so syncconf CLEARS the listen port and the private
	key. This is the exact wg-mesh key-vs-syncconf trap (verified on a real host). So the
	order is load-bearing: `syncconf` FIRST, then `wg set` the derived port + key LAST, from
	the 0600 key file (never inline). Run under `bash -c` for process substitution."""
	parent = GATEWAY_CONFIG_PATH.rsplit("/", 1)[0]
	_stdout, stderr, code = run_ssh(
		connection,
		key_path,
		"sudo install -d -m 0755 {} && sudo tee {} >/dev/null && sudo chmod 0600 {}",
		parent,
		GATEWAY_CONFIG_PATH,
		GATEWAY_CONFIG_PATH,
		timeout_seconds=60,
		stdin=desired,
	)
	if code != 0:
		frappe.throw(f"Writing wg0.conf to gateway {gateway_vm} failed (exit {code}): {stderr[-300:]}")
	# syncconf the peers FIRST (rewrites [Interface], clearing the unmentioned port + key),
	# THEN re-set the derived listen port + key from the 0600 file so they survive — the
	# order is load-bearing, exactly like host_mesh._apply_script.
	apply_script = (
		f"wg syncconf {GATEWAY_DEVICE} <(wg-quick strip {GATEWAY_CONFIG_PATH}); "
		f"wg set {GATEWAY_DEVICE} private-key {GATEWAY_KEY_PATH} listen-port {WG_GATEWAY_PORT}"
	)
	stdout, stderr, code = run_ssh(connection, key_path, "sudo bash -c {}", apply_script, timeout_seconds=120)
	if code != 0:
		frappe.throw(f"wg syncconf to gateway {gateway_vm} failed (exit {code}): {stderr[-500:]}")
	_ = stdout


def _read_gateway_public_key(connection, key_path, gateway_vm: str) -> str:
	"""The gateway's wg0 public key — read from the device (`wg show wg0 public-key`), the
	one key shared by every peer. Read from the gateway, never stored per row; it is minted
	once on the gateway at bake. Raises if the device isn't up (the gateway isn't ready)."""
	stdout, stderr, code = run_ssh(
		connection, key_path, f"sudo wg show {GATEWAY_DEVICE} public-key", timeout_seconds=30
	)
	if code != 0 or not stdout.strip():
		frappe.throw(
			f"Reading wg0 public key from gateway {gateway_vm} failed (exit {code}): {stderr[-300:]}"
		)
	return stdout.strip()


def _denormalize_server_public_key(gateway_vm: str, server_public_key: str) -> None:
	"""Denorm the gateway's shared wg0 public key onto every Active peer of this gateway
	whose stored value is stale, so the config dialog renders without re-SSHing. One key
	per gateway — this is a legibility denorm, not per-row identity."""
	for name in frappe.get_all(
		"VPN Peer",
		filters={"gateway": gateway_vm, "status": "Active", "server_public_key": ["!=", server_public_key]},
		pluck="name",
	):
		frappe.db.set_value("VPN Peer", name, "server_public_key", server_public_key)


def client_config_payload(peer) -> dict:
	"""The ready-to-use client payload: the copy-paste `.conf` + setup steps (reference
	§4). The two AllowedIPs are the single most confusing point, so they are commented
	inline. The customer's PrivateKey is a placeholder — Atlas never had it."""
	config = (
		"[Interface]\n"
		"PrivateKey = <your client private key — paste from your privatekey file>\n"
		f"Address    = {peer.client_address}/128\n"
		"\n"
		"[Peer]\n"
		f"PublicKey  = {peer.server_public_key or '<gateway public key — re-enroll to fetch>'}\n"
		f"Endpoint   = {peer.endpoint}\n"
		f"AllowedIPs = {peer.allowed_ips}\n"
		"PersistentKeepalive = 25\n"
	)
	instructions = (
		"1. On your machine, generate a keypair (you already did this to get the public key):\n"
		"     wg genkey | tee privatekey | wg pubkey > publickey\n"
		"2. Save the config above as /etc/wireguard/tenant-vpc.conf and paste your\n"
		"   privatekey contents into the PrivateKey line.\n"
		"3. Bring the tunnel up:\n"
		"     wg-quick up tenant-vpc\n"
		f"4. Reach any VM in your {peer.tenant} VPC by its fdaa: address, e.g.\n"
		"     ssh root@<vm fdaa: address>\n"
		"\n"
		"The customer-side AllowedIPs routes your whole VPC out the tunnel; you may narrow\n"
		"it. Editing it does NOT weaken isolation — the gateway accepts only your own /128\n"
		"as a source and drops any cross-tenant destination."
	)
	return {
		"config": config,
		"instructions": instructions,
		"client_address": peer.client_address,
		"endpoint": peer.endpoint,
		"allowed_ips": peer.allowed_ips,
	}
