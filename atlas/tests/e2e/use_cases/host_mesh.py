"""Use case: the WireGuard HOST mesh (private-plane fabric).

The design lives in `llm/references/private-networking-host-mesh.md`. Each Active
host peers with every other over the hosts' public IPv6 endpoints; a guest sends
plain IPv6 to its tap and the host encapsulates onto `wg-mesh`. This module proves
the host facts only two real cross-provider-edge hosts can — the ones the unit suite
(test_private_networking*.py) can NOT reach:

  Phase-0 gate #1 (THE gate — it gates the whole pivot): wg-over-UDP/51820 between
  two hosts' public IPv6 survives the DigitalOcean edge. Proven end-to-end by:
    1. `reconcile_host_mesh()` brings `wg-mesh` up on BOTH hosts, each carrying the
       other as a peer whose AllowedIPs include the peer's own infra mesh /128.
    2. Each host can PING the other's derived `mesh_address` over `wg-mesh` — a real
       encrypted round trip across the public v6 edge (the host↔host bus, §2.4).
    3. A >1420-byte ping across the mesh does not blackhole (PMTU clean at MTU 1420,
       gate #4).
  Gate #5 (defense-in-depth): a guest netns cannot see the `wg-mesh` device (it lives
  in the host root netns) — asserted structurally by listing links inside a VM's netns.

This is a **dedicated-two-droplet** host fact (like migration): it needs a source AND
a target Server, both Active + same-provider, so it owns its droplets and is invoked
DIRECTLY, not folded into `run_all_smoke`:

    bench --site atlas.tests.local execute atlas.tests.e2e.use_cases.host_mesh.run_smoke

Cost: up to two billable droplets (reuses any Active pair). `run_smoke` is the cheap
two-host core (facts 1-3). `run` adds two guest VMs to prove the per-VM data plane:
two SAME-tenant VMs on the two hosts reach each other's private /128; a DIFFERENT-
tenant VM cannot (the isolation drop — the negative is the point, design §9).

TEARDOWN: the mesh device is meant to be permanent, but on the shared e2e fleet other
use cases don't expect it, so (unless keep=True) we tear `wg-mesh` down on both hosts
in a `finally` — symmetric with how migration removes its tunnel. The Server rows'
derived denorm fields are read-through, so nothing DB-side needs cleanup.
"""

import ipaddress
import time

import frappe

from atlas.atlas.networking import WIREGUARD_MTU, derive_host_mesh_address, derive_private_address
from atlas.atlas.ssh import connection_for_server, run_ssh, ssh_key_file
from atlas.tests.e2e._droplets import ensure_two_active_servers
from atlas.tests.e2e._image import ensure_image_on_server
from atlas.tests.e2e._shared import ephemeral_public_key

MESH_DEVICE = "wg-mesh"
BOOT_TIMEOUT = 180


def host_shell(server_name: str, command: str, timeout: int = 40) -> str:
	"""Run a raw shell command on a Server HOST over the controller SSH key (the same
	primitive migration/proxy e2es use). Returns stdout."""
	conn = connection_for_server(frappe.get_doc("Server", server_name))
	with ssh_key_file(conn.ssh_private_key) as key_path:
		out, _err, _code = run_ssh(conn, key_path, command, timeout_seconds=timeout)
	return out


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	"""The two-host core (facts 1-3): reconcile the mesh onto two real hosts and prove
	host↔host reachability over the tunnel. No guest VMs — this is the cheapest proof of
	the highest-risk gate (#1). Reuses any Active pair; provisions what's missing."""
	source, target = ensure_two_active_servers(reuse=reuse, keep=keep)
	print(f"[e2e] host-mesh smoke: hosts {source.name} <-> {target.name}")

	try:
		_bring_up_and_verify_mesh(source.name, target.name)
		print("[e2e] host-mesh smoke OK: mesh up on both hosts, host↔host reachable over wg-mesh")
	finally:
		if not keep:
			_teardown_mesh(source.name, target.name)


def run(reuse: bool = True, keep: bool = True) -> None:
	"""The full path: `run_smoke`'s two-host mesh PLUS two guest VMs proving the per-VM
	data plane and tenant isolation (design §9, Phase 1):

	  4. Two SAME-tenant VMs, one on each host, reach each other's private /128 across
	     the mesh (the §2.3 guest packet path: tap→veth→wg-mesh→peer→veth→tap).
	  5. A DIFFERENT-tenant VM on the target CANNOT reach the source VM's private /128 —
	     the per-VM nft isolation drop (§4). The negative is the point.
	  6. A guest netns cannot see the `wg-mesh` device (it is in the host root ns) — gate #5.

	Heavy (boots up to three guest VMs); invoked directly, terminated in a finally."""
	source, target = ensure_two_active_servers(reuse=reuse, keep=keep)
	ensure_image_on_server(source.name)
	image = ensure_image_on_server(target.name)
	print(f"[e2e] host-mesh full: hosts {source.name} <-> {target.name}")

	vms: list[str] = []
	try:
		_bring_up_and_verify_mesh(source.name, target.name)

		tenant_a = _ensure_e2e_tenant("host-mesh-tenant-a")
		tenant_b = _ensure_e2e_tenant("host-mesh-tenant-b")

		# Two same-tenant VMs, one per host; one cross-tenant VM on the target.
		vm_a_src = _provision_tenant_vm(source.name, image.name, tenant_a, "mesh-a-src")
		vms.append(vm_a_src.name)
		vm_a_tgt = _provision_tenant_vm(target.name, image.name, tenant_a, "mesh-a-tgt")
		vms.append(vm_a_tgt.name)
		vm_b_tgt = _provision_tenant_vm(target.name, image.name, tenant_b, "mesh-b-tgt")
		vms.append(vm_b_tgt.name)

		# The mesh must now advertise all three /128s. Reconcile again (a provision would
		# have enqueued this; run it inline so the e2e is deterministic, not worker-timed).
		from atlas.atlas.host_mesh import reconcile_host_mesh

		reconcile_host_mesh()

		for vm in (vm_a_src, vm_a_tgt, vm_b_tgt):
			_wait_for_boot(vm.name, vm.server, vm.ipv6_address)

		# Fact 4: same-tenant cross-host reachability over the private plane. Ping the
		# source VM's private /128 FROM the target VM's guest (a real §2.3 round trip).
		reachable = _guest_ping(target.name, vm_a_tgt.ipv6_address, vm_a_src.private_address)
		assert reachable, (
			f"same-tenant VM {vm_a_tgt.name} could not reach {vm_a_src.name} at "
			f"{vm_a_src.private_address} across the mesh"
		)

		# Fact 5: cross-tenant isolation — the tenant-B VM must NOT reach tenant-A's /128.
		blocked = not _guest_ping(target.name, vm_b_tgt.ipv6_address, vm_a_src.private_address)
		assert blocked, (
			f"ISOLATION BREACH: cross-tenant VM {vm_b_tgt.name} reached {vm_a_src.name} at "
			f"{vm_a_src.private_address} — the §4 nft drop failed"
		)

		# Fact 6: the wg-mesh device is invisible from inside a guest netns (gate #5).
		_assert_mesh_invisible_in_guest(target.name, vm_a_tgt.name)

		print(
			"[e2e] host-mesh full OK: same-tenant cross-host reach ✓, cross-tenant drop ✓, "
			"wg-mesh invisible in guest ✓"
		)
	finally:
		if not keep:
			for name in vms:
				try:
					frappe.get_doc("Virtual Machine", name).terminate()
					frappe.db.commit()
				except Exception as exception:  # best-effort teardown
					print(f"[e2e] teardown: could not terminate {name}: {exception}")
			_teardown_mesh(source.name, target.name)


# --- the mesh host facts (shared by smoke + full) ----------------------------------


def _bring_up_and_verify_mesh(source: str, target: str) -> None:
	"""Reconcile the mesh onto both hosts and assert facts 1-3."""
	from atlas.atlas.host_mesh import reconcile_host_mesh

	# Fact 1: reconcile brings wg-mesh up on both hosts with the correct peer set.
	reconcile_host_mesh()
	frappe.db.commit()

	source_mesh = derive_host_mesh_address(source)
	target_mesh = derive_host_mesh_address(target)

	for host, peer, peer_mesh in ((source, target, target_mesh), (target, source, source_mesh)):
		_assert_device_up(host)
		_assert_peer_advertised(host, peer, peer_mesh)

	# Fact 2: each host reaches the OTHER's mesh address over wg-mesh — the real
	# encrypted round trip across the public-v6 edge (gate #1, end to end).
	_assert_mesh_ping(source, target_mesh, size=None)
	_assert_mesh_ping(target, source_mesh, size=None)

	# Fact 3: a >1420-byte ping does not blackhole (PMTU clean at MTU 1420, gate #4).
	_assert_mesh_ping(source, target_mesh, size=1600)
	_assert_mesh_ping(target, source_mesh, size=1600)


def _assert_device_up(host: str) -> None:
	"""wg-mesh exists, is UP, carries the derived infra /128, and has MTU 1420. Uses
	`ip link show` for existence + MTU (it lists the device even with no address, unlike
	`ip -6 addr show dev`, which some iproute2 builds print empty when the device has no
	v6 address) and `ip -6 addr show` for the assigned mesh address."""
	link = host_shell(host, f"ip link show dev {MESH_DEVICE} 2>/dev/null || echo MISSING")
	assert "MISSING" not in link, f"{MESH_DEVICE} not present on {host}"
	assert f"mtu {WIREGUARD_MTU}" in link, f"{MESH_DEVICE} on {host} is not MTU {WIREGUARD_MTU}: {link}"
	mesh_address = derive_host_mesh_address(host)
	addr = host_shell(host, f"ip -6 addr show dev {MESH_DEVICE} 2>/dev/null || echo NOADDR")
	assert mesh_address in addr, f"{host} {MESH_DEVICE} missing its infra address {mesh_address}: {addr}"
	route = host_shell(host, "ip -6 route show fdaa::/16 2>/dev/null || echo NOROUTE")
	assert MESH_DEVICE in route, f"{host} has no fdaa::/16 route via {MESH_DEVICE}: {route}"


def _assert_peer_advertised(host: str, peer: str, peer_mesh: str) -> None:
	"""The live `wg show wg-mesh dump` on `host` lists `peer` (by its derived pubkey)
	with the peer's own infra mesh /128 in AllowedIPs (§2.4) — so the host↔host bus can
	dial it."""
	from atlas.atlas.networking import derive_host_wireguard_keypair

	dump = host_shell(host, f"sudo wg show {MESH_DEVICE} dump 2>/dev/null || echo NODEV")
	assert "NODEV" not in dump, f"{MESH_DEVICE} has no wg device on {host}"
	_private, peer_pubkey = derive_host_wireguard_keypair(peer)
	assert peer_pubkey in dump, f"{host} does not carry peer {peer} (pubkey {peer_pubkey}) in wg dump"
	assert f"{peer_mesh}/128" in dump, (
		f"{host}'s peer {peer} is missing its infra mesh {peer_mesh}/128 in AllowedIPs: {dump}"
	)


def _assert_mesh_ping(host: str, target_mesh: str, size: int | None) -> None:
	"""Ping `target_mesh` FROM `host` over wg-mesh. size=None → default; an int → a
	payload that forces a >1420 packet (PMTU gate). Fail-loud with the raw output."""
	size_flag = f"-s {size} " if size else ""
	label = f"{size}B " if size else ""
	out = host_shell(
		host,
		f"ping -c 3 -W 3 {size_flag}{target_mesh} >/dev/null 2>&1 && echo REACHABLE || echo UNREACHABLE",
		timeout=30,
	)
	assert "REACHABLE" in out, (
		f"host {host} could NOT reach peer mesh {target_mesh} over {MESH_DEVICE} "
		f"({label}ping) — Phase-0 gate #1 (wg-over-UDP/51820 across the edge) FAILED"
	)


def _teardown_mesh(source: str, target: str) -> None:
	"""Remove wg-mesh from both hosts so the shared e2e fleet returns to a clean state
	(other use cases don't expect a mesh). Best-effort + idempotent."""
	for host in (source, target):
		try:
			host_shell(host, f"sudo ip link del {MESH_DEVICE} 2>/dev/null || true")
			host_shell(host, "sudo ip -6 route del fdaa::/16 2>/dev/null || true")
		except Exception as exception:
			print(f"[e2e] teardown: could not clean {MESH_DEVICE} on {host}: {exception}")


# --- guest data-plane facts (full run only) ----------------------------------------


def _ensure_e2e_tenant(name: str) -> str:
	if not frappe.db.exists("Tenant", name):
		frappe.get_doc({"doctype": "Tenant", "team": name, "email": f"{name}@e2e.test"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
	return name


def _provision_tenant_vm(server: str, image: str, tenant: str, title: str):
	"""Insert a tenant VM and let its after_insert auto_provision (the worker) bring it
	Running, then wait — do NOT also call vm.provision() here, or the inline provision
	races the worker's and both save the row (TimestampMismatchError). This is the same
	insert+commit+wait pattern reserved_ip_inbound uses. Needs the worker up."""
	from atlas.tests.e2e._tasks import wait_for_vm_running

	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": title,
			"server": server,
			"image": image,
			"tenant": tenant,
			"vcpus": 1,
			"memory_megabytes": 1024,
			"disk_gigabytes": 4,
			"ssh_public_key": ephemeral_public_key(),
		}
	).insert(ignore_permissions=True)
	assert vm.private_address, f"VM {vm.name} got no private_address despite tenant {tenant}"
	frappe.db.commit()
	wait_for_vm_running(vm.name, timeout_seconds=180)
	vm.reload()
	assert vm.status == "Running", f"VM {vm.name} did not reach Running: {vm.status}"
	return vm


def _wait_for_boot(vm_name: str, host: str, v6: str, timeout: int = BOOT_TIMEOUT) -> None:
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		out = host_shell(host, f"timeout 6 bash -c 'echo > /dev/tcp/{v6}/22' && echo OPEN || echo CLOSED")
		if "OPEN" in out:
			return
		time.sleep(5)
	raise AssertionError(f"VM {vm_name} not reachable on {v6}:22 within {timeout}s")


def _guest_ping(host: str, guest_v6: str, target_private: str) -> bool:
	"""SSH into the guest (from the host, over the guest's public v6) and ping the
	`target_private` fdaa:: address. Returns True iff reachable. The guest was injected
	with the ephemeral e2e keypair, so the host can reach it as root over its /128.

	UserKnownHostsFile=/dev/null + StrictHostKeyChecking=no: e2e droplets recycle guest
	/128s across runs, so a stale known_hosts entry would otherwise HARD-FAIL the ssh
	(REMOTE HOST IDENTIFICATION CHANGED) and land nowhere — silently poisoning the
	reachability verdict. The unique GUEST_OK sentinel with the expected hostname proves
	we actually reached THIS guest (not the host, if the /128 route were misconfigured)
	before trusting the ping verdict."""
	from atlas.tests.e2e._shared import ephemeral_private_key

	key = ephemeral_private_key()
	# The guest-side command is SINGLE-quoted so the OUTER host shell does not evaluate
	# $(hostname) or the &&/|| — those must run inside the guest, not on the host. (A
	# double-quoted guest command let the host shell mangle the verdict, producing a
	# false REACHABLE even when the ping failed.) `GUEST_OK=<hostname>` proves we landed
	# on the guest; `PING_REACHABLE`/`PING_BLOCKED` is the actual verdict from the guest.
	guest_cmd = (
		f"echo GUEST_OK=$(hostname); "
		f"ping -c 3 -W 3 {target_private} >/dev/null 2>&1 && echo PING_REACHABLE || echo PING_BLOCKED"
	)
	remote = (
		f"KEY=$(mktemp); cat > $KEY <<'EOF'\n{key}\nEOF\n"
		f"chmod 600 $KEY; "
		f"ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
		f"-o ConnectTimeout=10 root@{guest_v6} '{guest_cmd}' 2>/dev/null; rm -f $KEY"
	)
	out = host_shell(host, remote, timeout=60)
	if "GUEST_OK=atlas-" not in out:
		raise AssertionError(f"ssh to guest {guest_v6} did not land on a guest VM: {out[-300:]}")
	return "PING_REACHABLE" in out


def _assert_mesh_invisible_in_guest(host: str, vm_name: str) -> None:
	"""The wg-mesh device lives in the host ROOT netns; a guest's netns must not see it
	(gate #5). The VM's netns name is derived; list its links and assert wg-mesh absent."""
	from atlas.atlas.networking import derive_netns

	netns = derive_netns(vm_name)
	out = host_shell(host, f"sudo ip netns exec {netns} ip link show 2>/dev/null || echo NONS")
	assert MESH_DEVICE not in out, (
		f"ISOLATION BREACH: {MESH_DEVICE} is visible inside guest netns {netns} on {host}"
	)


def _ip_in_range(address: str, cidr: str) -> bool:
	return ipaddress.ip_address(address) in ipaddress.ip_network(cidr)
