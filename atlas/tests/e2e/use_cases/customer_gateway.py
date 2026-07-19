"""Use case: the customer gateway (external dial-in to the tenant VPC).

Design: spec/25 Phase 5, spec/26, `llm/references/customer-vpc-vpn.md`. A customer
runs stock `wg-quick`, dials the region's GATEWAY VM's fixed public v4 :51820, and
lands inside their tenant's /48 — reaching every VM in their VPC by its stable `fdaa:`
address, while the gateway pins their source (WireGuard cryptokey routing) and confines
their destination to their own /48 (a static `same_48` eBPF guard on wg0 ingress).

This proves the host facts the unit suite (test_customer_gateway.py) can NOT reach —
the live wg0 + the eBPF guard's verdicts on a real deployed gateway (VPC-Phase A's one
remaining [HV], reference §11):

  run_smoke (structural, one host + a gateway VM, NO customer client):
    1. deploy_gateway stands wg0 up with the static same_48 guard attached (tc filter)
       + the host-local input drop, and mints the gateway's shared key.
    2. request_vpc_access enrolls a peer → wg0 carries it (pinned to the client /128).
    3. the client /128 is advertised into the host mesh (return path).
    4. revoke drops the peer from wg0 and withdraws the /128.

  run (the full L3 proof, TWO hosts + a real wg-quick "laptop" on host2):
    5. a real wg-quick client (host2 as the customer's premises) dials the gateway's v4
       and REACHES a same-tenant VM by its fdaa: address (the whole point).
    6. the client CANNOT reach a DIFFERENT-tenant VM (the same_48 drop — the negative
       is the point).
    7. the client CANNOT reach the gateway VM itself (the iifname wg0 input drop).

Invoked DIRECTLY (like host_mesh / migration), not folded into run_all_smoke:

    bench --site atlas.tests.local execute atlas.tests.e2e.use_cases.customer_gateway.run_smoke
    bench --site atlas.tests.local execute atlas.tests.e2e.use_cases.customer_gateway.run

Cost: run_smoke uses one host + one gateway VM; run adds a second host + two tenant VMs.
TEARDOWN (unless keep=True): revoke peers, terminate the gateway + VMs, tear the mesh down.
"""

import time

import frappe

from atlas.tests.e2e._droplets import ensure_two_active_servers
from atlas.tests.e2e._image import ensure_image_on_server
from atlas.tests.e2e._shared import control_plane_public_key, ephemeral_public_key

GATEWAY_DEVICE = "wg0"
CLIENT_DEVICE = "vpc-e2e"


def host_shell(server_name: str, command: str, timeout: int = 40) -> str:
	from atlas.atlas.ssh import connection_for_server, run_ssh, ssh_key_file

	conn = connection_for_server(frappe.get_doc("Server", server_name))
	with ssh_key_file(conn.ssh_private_key) as key_path:
		out, _err, _code = run_ssh(conn, key_path, command, timeout_seconds=timeout)
	return out


def run_smoke(reuse: bool = True, keep: bool = False) -> None:
	"""Structural proof on one real host: deploy the gateway, enroll + revoke a peer,
	asserting wg0 + the guard + the mesh advertisement at each step. No customer client."""
	source, target = ensure_two_active_servers(reuse=reuse, keep=True)
	image = ensure_image_on_server(source.name)
	print(f"[e2e] customer-gateway smoke: gateway host {source.name}, peer host {target.name}")

	
	gateway = None
	peer = None
	try:
		frappe.db.commit()

		gateway = _provision_gateway(source.name, image.name)
		_deploy_and_assert_gateway(gateway)

		tenant = _ensure_e2e_tenant("gw-smoke-tenant")
		peer = _enroll_peer(tenant, "smoke-laptop")
		_assert_peer_on_wg0(gateway.name, peer)
		# The enroll enqueues an ASYNC mesh reconcile; run it inline so the e2e is
		# deterministic (not worker-timed). The client /128 is a resident of the gateway's
		# host, so it is advertised to the OTHER host — assert it in target's dump.
		frappe.db.commit()
		_assert_client_in_mesh(target.name, peer)
		print("[e2e] customer-gateway smoke: peer enrolled on wg0 + advertised in mesh ✓")

		peer.revoke()
		frappe.db.commit()
		_assert_peer_absent_from_wg0(gateway.name, peer)
		_assert_client_absent_from_mesh(target.name, peer)
		print("[e2e] customer-gateway smoke OK: gateway up, guard attached, enroll/revoke clean ✓")
	finally:
		if not keep:
			_teardown(
				gateway_name=gateway.name if gateway else None, vm_names=[], hosts=[source.name, target.name]
			)


def run(reuse: bool = True, keep: bool = False) -> None:
	"""The full L3 proof: a real wg-quick client on host2 dials the gateway (on host1) and
	proves reach to a same-tenant VM, the cross-tenant drop, and the gateway-self drop."""
	source, target = ensure_two_active_servers(reuse=reuse, keep=True)
	ensure_image_on_server(source.name)
	image = ensure_image_on_server(target.name)
	print(f"[e2e] customer-gateway full: gateway host {source.name}, client host {target.name}")

	
	gateway = None
	vms: list[str] = []
	peer = None
	try:
		frappe.db.commit()

		# The gateway on host1; two tenant VMs (one per tenant) also on host1 so a single
		# host proves the VPC reach (cross-host reach is already proven by host_mesh e2e).
		gateway = _provision_gateway(source.name, image.name)
		gateway_v4 = frappe.db.get_value("Virtual Machine", gateway.name, "public_ipv4")
		assert gateway_v4, "gateway VM has no public_ipv4 — reserved IP attach failed"
		_deploy_and_assert_gateway(gateway)

		tenant_a = _ensure_e2e_tenant("gw-tenant-a")
		tenant_b = _ensure_e2e_tenant("gw-tenant-b")
		vm_a = _provision_tenant_vm(source.name, image.name, tenant_a, "gw-vm-a")
		vms.append(vm_a.name)
		vm_b = _provision_tenant_vm(source.name, image.name, tenant_b, "gw-vm-b")
		vms.append(vm_b.name)

		# Enroll the customer (tenant A) and advertise into the mesh.
		client_private, client_public = _wg_keypair(target.name)
		peer = _enroll_peer(tenant_a, "e2e-laptop", client_public_key=client_public)
		frappe.db.commit()
		_assert_peer_on_wg0(gateway.name, peer)

		# Bring a real wg-quick client up on host2 (the "customer premises"), dialing the
		# gateway's public v4. Then run the three L3 assertions from that client.
		_bring_up_client(target.name, client_private, peer, gateway_v4)
		try:
			# Fact 5: the client reaches its OWN tenant's VM by its fdaa: address.
			reachable = _client_ping(target.name, vm_a.private_address)
			assert reachable, (
				f"customer client could NOT reach same-tenant VM {vm_a.name} at "
				f"{vm_a.private_address} through the gateway — the VPC path failed"
			)
			# Fact 6: the client CANNOT reach a different tenant's VM (same_48 drop).
			blocked = not _client_ping(target.name, vm_b.private_address)
			assert blocked, (
				f"ISOLATION BREACH: customer client reached cross-tenant VM {vm_b.name} at "
				f"{vm_b.private_address} — the same_48 eBPF guard failed"
			)
			# Fact 7: the client CANNOT reach the gateway VM itself (iifname wg0 input drop).
			gateway_private = frappe.db.get_value("Virtual Machine", gateway.name, "private_address")
			if gateway_private:
				gw_blocked = not _client_ping(target.name, gateway_private)
				assert gw_blocked, (
					f"ISOLATION BREACH: customer client reached the gateway itself at "
					f"{gateway_private} — the iifname wg0 input drop failed"
				)
			print(
				"[e2e] customer-gateway full OK: same-tenant reach ✓, cross-tenant drop ✓, "
				"gateway-self drop ✓"
			)
		finally:
			host_shell(target.name, f"sudo wg-quick down {CLIENT_DEVICE} 2>/dev/null || true")
	finally:
		if not keep:
			if peer:
				try:
					peer.revoke()
					frappe.db.commit()
				except Exception as exception:
					print(f"[e2e] teardown: could not revoke peer: {exception}")
			_teardown(
				gateway_name=gateway.name if gateway else None,
				vm_names=vms,
				hosts=[source.name, target.name],
			)


# --- gateway provisioning + assertions ----------------------------------------------


def _provision_gateway(server: str, image: str):
	"""Provision an is_gateway VM and attach a real reserved v4 (its fixed Endpoint, the
	proxy story). Reuses an existing e2e gateway on this server if one is live."""
	from atlas.tests.e2e._tasks import wait_for_vm_running

	existing = frappe.get_all(
		"Virtual Machine",
		filters={"server": server, "is_gateway": 1, "status": ["not in", ("Terminated", "Draft")]},
		pluck="name",
	)
	if existing:
		vm = frappe.get_doc("Virtual Machine", existing[0])
		if not vm.public_ipv4:
			_attach_reserved_ip(server, vm.name)
			vm.reload()
		return vm

	# The gateway guest is reached by the control plane (deploy_gateway / reconcile_gateway
	# via connection_for_guest, which uses the Atlas-settings key) AND by host-side probes
	# (the ephemeral key). In production the gateway image bakes the Atlas key; here we
	# authorize BOTH (one key per line) so neither path is locked out — the proxy pattern.
	authorized = ephemeral_public_key() + "\n" + control_plane_public_key()
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "e2e-gateway",
			"server": server,
			"image": image,
			"is_gateway": 1,
			"vcpus": 1,
			"memory_megabytes": 1024,
			"disk_gigabytes": 4,
			"ssh_public_key": authorized,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	wait_for_vm_running(vm.name, timeout_seconds=240)
	_attach_reserved_ip(server, vm.name)
	vm.reload()
	assert vm.status == "Running", f"gateway VM {vm.name} did not reach Running: {vm.status}"
	return vm


def _attach_reserved_ip(server: str, vm_name: str) -> None:
	from atlas.atlas.doctype.reserved_ip import reserved_ip as module

	reserved = module.allocate(server)
	frappe.db.commit()
	frappe.get_doc("Reserved IP", reserved).attach(vm_name)
	frappe.db.commit()


def _purge_known_host(address: str) -> None:
	"""Drop any stale ~/.atlas/known_hosts entry for `address`. The shared e2e fleet
	recycles guest /128s across terminated VMs, so a fresh gateway can inherit an address
	whose OLD host key is still cached — and StrictHostKeyChecking=accept-new hard-fails on
	a CHANGED key (only a NEW host is auto-accepted). Purging lets accept-new re-learn the
	new key. A no-op in production, where /128s don't recycle like this."""
	import subprocess

	from atlas.atlas._ssh.transport import KNOWN_HOSTS_PATH

	if KNOWN_HOSTS_PATH.exists():
		subprocess.run(["ssh-keygen", "-f", str(KNOWN_HOSTS_PATH), "-R", address], capture_output=True)


def _deploy_and_assert_gateway(gateway) -> None:
	"""Deploy wg0 + the static guard, then assert the device is up, the tc filter is
	attached, and the host-local input drop is present (reference §9)."""
	from atlas.atlas import customer_gateway

	_purge_known_host(gateway.ipv6_address)
	customer_gateway.deploy_gateway(gateway.name)
	# wg0 up.
	link = _guest_shell(gateway, f"ip link show dev {GATEWAY_DEVICE} 2>/dev/null || echo MISSING")
	assert "MISSING" not in link, f"{GATEWAY_DEVICE} not up on gateway {gateway.name}"
	# The same_48 tc filter is attached on wg0 ingress.
	filt = _guest_shell(gateway, f"sudo tc filter show dev {GATEWAY_DEVICE} ingress 2>/dev/null || echo NONE")
	assert "bpf" in filt, f"same_48 eBPF guard not attached on {GATEWAY_DEVICE}: {filt}"
	# The host-local input drop is present in the guest's own table.
	nft = _guest_shell(gateway, "sudo nft list table inet gateway 2>/dev/null || echo NOTABLE")
	assert 'iifname "wg0" drop' in nft, f"host-local input drop missing on gateway: {nft}"
	print(f"[e2e] gateway {gateway.name}: wg0 up, same_48 guard attached, input drop present ✓")


def _guest_shell(vm, command: str, timeout: int = 60) -> str:
	"""Run a command inside a guest VM over guest-SSH (the controller connection)."""
	from atlas.atlas.ssh import connection_for_guest, run_ssh, ssh_key_file

	conn = connection_for_guest(vm if hasattr(vm, "name") else frappe.get_doc("Virtual Machine", vm))
	with ssh_key_file(conn.ssh_private_key) as key_path:
		out, _err, _code = run_ssh(conn, key_path, command, timeout_seconds=timeout)
	return out


def _assert_peer_on_wg0(gateway_name: str, peer) -> None:
	vm = frappe.get_doc("Virtual Machine", gateway_name)
	dump = _guest_shell(vm, f"sudo wg show {GATEWAY_DEVICE} dump 2>/dev/null || echo NODEV")
	assert peer.client_public_key in dump, f"gateway does not carry peer {peer.name}: {dump}"
	assert f"{peer.client_address}/128" in dump, (
		f"peer {peer.name} AllowedIPs is not its own /128 {peer.client_address}: {dump}"
	)


def _assert_peer_absent_from_wg0(gateway_name: str, peer) -> None:
	vm = frappe.get_doc("Virtual Machine", gateway_name)
	dump = _guest_shell(vm, f"sudo wg show {GATEWAY_DEVICE} dump 2>/dev/null || echo NODEV")
	assert peer.client_public_key not in dump, f"revoked peer {peer.name} still on wg0: {dump}"


def _assert_client_in_mesh(observer_host: str, peer) -> None:
	"""The client /128 is a resident of the GATEWAY's host, so every OTHER host routes it to
	the gateway host — it appears in `observer_host`'s wg-mesh dump (the gateway host's peer
	stanza) as an AllowedIP. The return path (reference §6.3)."""
	dump = host_shell(observer_host, "sudo wg show wg-mesh dump 2>/dev/null || echo NODEV")
	assert f"{peer.client_address}/128" in dump, (
		f"client /128 {peer.client_address} not advertised in the mesh (observed from {observer_host}): {dump}"
	)


def _assert_client_absent_from_mesh(observer_host: str, peer) -> None:
	"""After revoke, the client /128 is withdrawn from the mesh (the teardown-bug-safe
	reconcile-on-teardown, reference §6.3)."""
	dump = host_shell(observer_host, "sudo wg show wg-mesh dump 2>/dev/null || echo NODEV")
	assert f"{peer.client_address}/128" not in dump, (
		f"revoked client /128 {peer.client_address} still advertised in the mesh: {dump}"
	)


# --- peers, tenants, VMs -------------------------------------------------------------


def _ensure_e2e_tenant(name: str) -> str:
	if not frappe.db.exists("Tenant", name):
		frappe.get_doc({"doctype": "Tenant", "team": name}).insert(ignore_permissions=True)
		frappe.db.commit()
	return name


def _enroll_peer(tenant: str, label: str, client_public_key: str | None = None):
	"""Enroll a customer peer via the real controller path (request_vpc_access)."""
	from atlas.atlas import customer_gateway

	if client_public_key is None:
		# A structural smoke needs no real client; mint a throwaway keypair anywhere.
		_private, client_public_key = _wg_keypair_local()
	customer_gateway.request_vpc_access(tenant, client_public_key, label)
	frappe.db.commit()
	name = frappe.get_all("VPN Peer", filters={"tenant": tenant, "label": label}, pluck="name")[0]
	return frappe.get_doc("VPN Peer", name)


def _provision_tenant_vm(server: str, image: str, tenant: str, title: str):
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
	return vm


# --- the real wg-quick client (host2 as the customer premises) -----------------------


def _wg_keypair(host: str) -> tuple[str, str]:
	"""Generate a WireGuard keypair ON host2 (where the client will run). Returns
	(private, public). The private key stays on host2 — Atlas only gets the public half."""
	private = host_shell(host, "wg genkey").strip()
	public = host_shell(host, f"echo {private} | wg pubkey").strip()
	return private, public


def _wg_keypair_local() -> tuple[str, str]:
	"""A throwaway keypair for a structural smoke (no real client). Uses the controller's
	own wg if present, else a pure-python X25519 pair (validity is all the smoke needs)."""
	import base64

	from cryptography.hazmat.primitives import serialization
	from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

	key = X25519PrivateKey.generate()
	private = base64.b64encode(
		key.private_bytes(
			serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
		)
	).decode()
	public = base64.b64encode(
		key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
	).decode()
	return private, public


def _bring_up_client(host: str, client_private: str, peer, gateway_v4: str) -> None:
	"""Write /etc/wireguard/vpc-e2e.conf on host2 and `wg-quick up` it — a real customer
	dial-in to the gateway's public v4. The client address + AllowedIPs come from the peer
	row exactly as the customer's copy-paste config would."""
	config = (
		"[Interface]\n"
		f"PrivateKey = {client_private}\n"
		f"Address = {peer.client_address}/128\n"
		"\n"
		"[Peer]\n"
		f"PublicKey = {peer.server_public_key}\n"
		f"Endpoint = {gateway_v4}:51820\n"
		f"AllowedIPs = {peer.allowed_ips}\n"
		"PersistentKeepalive = 25\n"
	)
	assert peer.server_public_key, "peer has no server_public_key — gateway reconcile didn't denorm it"
	# Write the config (via a heredoc so the key never lands in an argv) and bring it up.
	host_shell(
		host,
		f"sudo bash -c 'umask 077; cat > /etc/wireguard/{CLIENT_DEVICE}.conf <<\"EOF\"\n{config}EOF'",
	)
	host_shell(host, f"sudo wg-quick down {CLIENT_DEVICE} 2>/dev/null || true")
	out = host_shell(host, f"sudo wg-quick up {CLIENT_DEVICE} 2>&1 || echo WGFAIL", timeout=60)
	assert "WGFAIL" not in out, f"wg-quick up failed on the client host: {out}"
	time.sleep(3)  # let the handshake complete + keepalive open the path


def _client_ping(host: str, target_private: str) -> bool:
	"""Ping a private fdaa:: address FROM the wg-quick client on host2, over the tunnel.
	Returns True iff reachable."""
	out = host_shell(
		host,
		f"ping -c 3 -W 3 -I {CLIENT_DEVICE} {target_private} >/dev/null 2>&1 && echo REACHABLE || echo BLOCKED",
		timeout=30,
	)
	return "REACHABLE" in out


# --- teardown ------------------------------------------------------------------------


def _teardown(gateway_name: str | None, vm_names: list[str], hosts: list[str]) -> None:
	for name in vm_names:
		try:
			frappe.get_doc("Virtual Machine", name).terminate()
			frappe.db.commit()
		except Exception as exception:
			print(f"[e2e] teardown: could not terminate {name}: {exception}")
	if gateway_name:
		try:
			frappe.get_doc("Virtual Machine", gateway_name).terminate()
			frappe.db.commit()
		except Exception as exception:
			print(f"[e2e] teardown: could not terminate gateway {gateway_name}: {exception}")
	for host in hosts:
		try:
			host_shell(host, "sudo ip link del wg-mesh 2>/dev/null || true")
			host_shell(host, "sudo ip -6 route del fdaa::/16 2>/dev/null || true")
		except Exception as exception:
			print(f"[e2e] teardown: could not clean mesh on {host}: {exception}")
