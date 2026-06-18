"""Use case: provision a Firecracker host on Scaleway Elastic Metal.

This is the host-bound go/no-go for the Scaleway provider — the class of facts
the e2e suite is the source of truth for (the provider logic is unit-proven in
`atlas/atlas/providers/test_scaleway.py` in milliseconds). It provisions a REAL
bare-metal server (hourly billing), so it owns its own server and tears it down
in a `finally`.

It runs the steps the unit tests cannot:

1. **Fresh provision** — `provision_server` → async create+install → the worker
   polls `describe()` to `ready`+`completed` (bare-metal install, minutes not
   seconds) → SSH as root → `bootstrap-server.py` → Server Active. Proves the
   async two-phase shape and the longer ready timeout.
2. **LVM thin pool** — assert `bootstrap-server.py` built the pool (the
   real-NVMe-device backing, §8, is a separate slice — this prints the backing).
3. **The CRITICAL IPv6 gate** (§6) — provision one Firecracker VM and reach its
   `/128` (from the routed flexible `/64`) from an external v6 client, proving
   proxy-NDP + routed-tap works on Scaleway's pure-L3 path WITHOUT a Virtual MAC.
   This is the whole provider's go/no-go.
4. **Flexible IP inbound v4** (§7) — allocate → attach a FIP, reach the guest
   over it through the host 1:1-NAT (`62.210.0.1` gateway, no anchor), detach,
   release.

Run it directly (it is not wired into `run_all`, which is DO-only):

    bench --site atlas.tests.local execute \
      atlas.tests.e2e.use_cases.scaleway_provisioning.run

By default it KEEPS the server + FIPs running (the operator drops them by hand);
pass `keep=False` to tear the server down at the end.
"""

import subprocess
import time
import traceback

import frappe

from atlas.atlas.ssh import run_task
from atlas.tests.e2e._config import ephemeral_private_key, ephemeral_public_key
from atlas.tests.e2e._scaleway import (
	cleanup_scaleway_server,
	ensure_scaleway_provider,
	sweep_old_scaleway_servers,
)
from atlas.tests.e2e._tasks import wait_for_vm_running

READY_TIMEOUT_SECONDS = 2400  # bare-metal install: minutes, up to ~1h worst case


def run(reuse: bool = True, keep: bool = True) -> None:
	"""Full Scaleway host bring-up + the IPv6/FIP gates.

	`reuse` returns an already-Active Scaleway Server if one is SSH-reachable
	(so a re-run doesn't re-provision a fresh bare-metal box, which is slow and
	billable). `keep=True` (default) leaves the server + any allocated FIPs
	running for the operator to drop manually.
	"""
	start = time.monotonic()
	sweep_old_scaleway_servers()
	provider = ensure_scaleway_provider()

	server, created_now = _ensure_active_server(provider, reuse=reuse)
	try:
		_assert_bootstrap_succeeded(server.name)
		_assert_pool_present(server.name)
		_run_ipv6_guest_gate(server.name)
		_run_flexible_ip_gate(server.name)
	except Exception:
		print(f"scaleway-provisioning: FAIL in {time.monotonic() - start:.0f}s")
		traceback.print_exc()
		raise
	finally:
		if created_now and not keep and server.provider_resource_id:
			cleanup_scaleway_server(server.provider_resource_id)

	print(f"scaleway-provisioning: OK in {time.monotonic() - start:.0f}s")
	print(
		f"[e2e/scw] server {server.name} ({server.ipv4_address}) left RUNNING — "
		f"drop it with: bench execute atlas.tests.e2e.use_cases.scaleway_provisioning.teardown"
	)


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	"""Same as `run` — the whole use case is host-bound, no extra unit-coverable
	layer to peel off (kept for symmetry with the other use cases)."""
	run(reuse=reuse, keep=keep)


def provision_only(reuse: bool = True) -> None:
	"""Just bring up the bare-metal host to Active + assert bootstrap — no VM
	gates, no teardown. Splits the slow/billable provision from the networking
	gates so the host can be inspected (or the gates re-run) without
	re-provisioning. Leaves the server running.

	    bench --site atlas.tests.local execute \
	      atlas.tests.e2e.use_cases.scaleway_provisioning.provision_only
	"""
	sweep_old_scaleway_servers()
	provider = ensure_scaleway_provider()
	server, _created = _ensure_active_server(provider, reuse=reuse)
	_assert_bootstrap_succeeded(server.name)
	_assert_pool_present(server.name)
	print(f"[e2e/scw] host {server.name} Active at {server.ipv4_address} — bootstrap + pool OK")


# ----- server bring-up ------------------------------------------------------


def _ensure_active_server(provider, reuse: bool):
	"""Return (server_doc, created_now). Reuse an Active+reachable Scaleway
	server, else provision a fresh bare-metal box and poll it to Active."""
	if reuse:
		existing = frappe.get_all(
			"Server",
			filters={"status": "Active", "provider": provider.name},
			pluck="name",
		)
		for name in existing:
			if _server_is_reachable(name):
				print(f"[e2e/scw] reusing Active server {name}")
				return frappe.get_doc("Server", name), False

	title = f"atlas-e2e-scw-{int(time.time())}"
	print(f"[e2e/scw] provisioning {title!r} (bare-metal install can take minutes)")
	server_name = provider.provision_server(title)
	server = _wait_for_status(server_name, {"Active", "Broken"}, READY_TIMEOUT_SECONDS)
	if server.status != "Active":
		raise AssertionError(f"server {server_name} ended {server.status}, expected Active")
	print(f"[e2e/scw] server {server_name} Active, ipv4={server.ipv4_address}")
	return server, True


def _wait_for_status(server_name: str, target: set[str], timeout: int):
	deadline = time.monotonic() + timeout
	last = None
	while time.monotonic() < deadline:
		frappe.db.rollback()
		server = frappe.get_doc("Server", server_name)
		if server.status != last:
			print(f"[e2e/scw] {server_name} status={server.status} ipv4={server.ipv4_address}")
			last = server.status
		if server.status in target:
			return server
		time.sleep(15)
	raise AssertionError(f"server {server_name} did not reach {target} within {timeout}s")


def _server_is_reachable(server_name: str, timeout_seconds: int = 5) -> bool:
	from atlas.atlas.ssh import connection_for_server, wait_for_ssh

	server = frappe.get_doc("Server", server_name)
	if not server.ipv4_address:
		return False
	try:
		wait_for_ssh(connection_for_server(server), timeout_seconds=timeout_seconds, poll_seconds=1)
		return True
	except Exception:
		return False


def _assert_bootstrap_succeeded(server_name: str) -> None:
	tasks = frappe.get_all(
		"Task",
		filters={"server": server_name, "script": "bootstrap-server.py", "status": "Success"},
	)
	assert tasks, "no successful bootstrap-server.py Task — host bring-up did not complete"
	server = frappe.get_doc("Server", server_name)
	assert server.firecracker_version, "firecracker_version not recorded"
	assert server.jailer_version, "jailer_version not recorded"


def _assert_pool_present(server_name: str) -> None:
	"""Read back the LVM thin pool: the atlas VG and pool0 thin LV exist and the
	reboot-survival oneshot is enabled. Prints the probe output so the operator
	can see the PV backing (loopback vs NVMe device) at a glance — the
	real-device-on-NVMe gate (§8) is a separate slice."""
	task = run_task(server=server_name, script="phase-pool-present.sh", variables={}, timeout_seconds=60)
	assert task.status == "Success", task.stderr
	assert "POOL PROBE OK" in task.stdout, task.stdout
	print(f"[e2e/scw] pool probe output:\n{task.stdout}")


# ----- the IPv6 go/no-go gate -----------------------------------------------


def _run_ipv6_guest_gate(server_name: str) -> None:
	"""Provision one Firecracker VM and reach its /128 over public v6 from this
	controller, proving routed-tap + proxy-NDP works on Scaleway's pure-L3 path
	without a Virtual MAC. THE provider go/no-go."""
	from atlas.tests.e2e._image import ensure_image_on_server

	image = ensure_image_on_server(server_name).name
	vm = _provision_vm(server_name, image)
	try:
		_assert_guest_reachable_over_v6(vm)
		print(
			f"[e2e/scw] IPv6 GATE PASS: guest {vm.name} reachable at {vm.ipv6_address} over pure-L3 routed /64"
		)
	finally:
		_terminate_vm(vm.name)


def _provision_vm(server_name: str, image: str):
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "scw ipv6 gate",
			"server": server_name,
			"image": image,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": ephemeral_public_key(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	wait_for_vm_running(vm.name, timeout_seconds=180)
	vm.reload()
	assert vm.status == "Running", vm.status
	assert vm.ipv6_address, "VM has no ipv6_address"
	return vm


def _assert_guest_reachable_over_v6(vm) -> None:
	"""SSH to the guest's /128 over public v6 from the controller and run
	`hostname`; assert it is this guest's atlas-<uuid8>. Loud on failure: if the
	NDP-proxy / routed-tap path does not carry on Scaleway, this is where it
	shows."""
	expected = f"atlas-{vm.name[:8]}"
	key_path = _controller_key_path()
	deadline = time.monotonic() + 180
	last_error = ""
	while time.monotonic() < deadline:
		try:
			result = subprocess.run(
				[
					"ssh",
					"-i",
					key_path,
					"-o",
					"StrictHostKeyChecking=no",
					"-o",
					"UserKnownHostsFile=/dev/null",
					"-o",
					"BatchMode=yes",
					"-o",
					"ConnectTimeout=10",
					f"root@{vm.ipv6_address}",
					"hostname",
				],
				capture_output=True,
				text=True,
				timeout=30,
			)
			if result.returncode == 0:
				actual = result.stdout.strip()
				assert actual == expected, f"v6 reached a guest but hostname={actual!r} want={expected!r}"
				print(f"[e2e/scw] v6 {vm.ipv6_address} -> {actual} OK")
				return
			last_error = (result.stderr or result.stdout).strip()
		except subprocess.TimeoutExpired:
			last_error = "ssh timed out"
		time.sleep(5)
	raise AssertionError(
		f"guest /128 {vm.ipv6_address} never reachable over public v6 within 180s "
		f"(last error: {last_error!r}). This is the Scaleway pure-L3 IPv6 go/no-go: either "
		f"NDP-proxy/routed-tap does not carry on Elastic Metal, the host /64 route is wrong, "
		f"or this controller has no outbound v6 path."
	)


# ----- the Flexible IP inbound-v4 gate --------------------------------------


def _run_flexible_ip_gate(server_name: str) -> None:
	"""Allocate + attach a Flexible IP to a VM, reach the guest over it through
	the host 1:1-NAT, then detach + release. Mirrors reserved_ip_inbound but on
	the Scaleway FIP primitive (gateway 62.210.0.1, no anchor)."""
	from atlas.atlas.doctype.reserved_ip import reserved_ip as module
	from atlas.tests.e2e._image import ensure_image_on_server

	image = ensure_image_on_server(server_name).name
	vm = _provision_vm(server_name, image)
	reserved = None
	try:
		reserved = module.allocate(server_name)
		frappe.db.commit()
		frappe.get_doc("Reserved IP", reserved).attach(vm.name)
		frappe.db.commit()
		fip_v4 = frappe.db.get_value("Reserved IP", reserved, "ip_address")
		_assert_inbound_reaches_guest(fip_v4, vm.name)
		print(f"[e2e/scw] FIP GATE PASS: inbound v4 {fip_v4} -> guest {vm.name} via 1:1-NAT")
	finally:
		_teardown_fip_and_vm(reserved, vm.name)


def _assert_inbound_reaches_guest(fip_v4: str, vm_name: str) -> None:
	expected = f"atlas-{vm_name[:8]}"
	key_path = _controller_key_path()
	deadline = time.monotonic() + 150
	last_error = ""
	while time.monotonic() < deadline:
		try:
			result = subprocess.run(
				[
					"ssh",
					"-i",
					key_path,
					"-o",
					"StrictHostKeyChecking=no",
					"-o",
					"UserKnownHostsFile=/dev/null",
					"-o",
					"BatchMode=yes",
					"-o",
					"ConnectTimeout=10",
					f"root@{fip_v4}",
					"hostname",
				],
				capture_output=True,
				text=True,
				timeout=30,
			)
			if result.returncode == 0:
				actual = result.stdout.strip()
				assert actual == expected, (
					f"inbound v4 reached a guest but hostname={actual!r} want={expected!r}"
				)
				print(f"[e2e/scw] inbound v4 {fip_v4} -> {actual} OK")
				return
			last_error = (result.stderr or result.stdout).strip()
		except subprocess.TimeoutExpired:
			last_error = "ssh timed out"
		time.sleep(5)
	raise AssertionError(
		f"inbound v4 to {fip_v4}:22 never reached the guest within 150s (last error: {last_error!r}). "
		f"Either the host 1:1-NAT DNAT is wrong, the FIP didn't bind, or no controller v4 path."
	)


def _teardown_fip_and_vm(reserved: str | None, vm_name: str) -> None:
	if reserved and frappe.db.exists("Reserved IP", reserved):
		try:
			doc = frappe.get_doc("Reserved IP", reserved)
			if doc.virtual_machine:
				doc.detach()
			doc.release()
			frappe.db.commit()
		except Exception:
			print(f"[e2e/scw] WARNING: FIP {reserved} teardown failed — release it by hand:")
			traceback.print_exc()
	_terminate_vm(vm_name)


def _terminate_vm(vm_name: str) -> None:
	if frappe.db.exists("Virtual Machine", vm_name):
		vm = frappe.get_doc("Virtual Machine", vm_name)
		if vm.status != "Terminated":
			vm.terminate()
			frappe.db.commit()


def _controller_key_path() -> str:
	import os

	directory = os.path.expanduser("~/.cache/atlas-e2e")
	os.makedirs(directory, exist_ok=True)
	path = os.path.join(directory, "scw-probe.key")
	with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
		handle.write(ephemeral_private_key())
	return path


def keep_flexible_ips(server_title: str = "") -> None:
	"""Allocate a standalone v4 Flexible IP + a free v6 /64 and attach BOTH to the
	e2e Scaleway box, leaving them up for the operator to exercise the IP-change
	path and drop later. Distinct from the FIP gate's ephemeral allocate/release.

	The v6 /64 is not modelled by the Reserved IP doctype (that primitive is
	v4-only), so both are allocated directly via the client here and left
	attached. Prints the FIP UUIDs + addresses so the operator can detach/delete.

	    bench --site atlas.tests.local execute \
	      atlas.tests.e2e.use_cases.scaleway_provisioning.keep_flexible_ips
	"""
	from atlas.tests.e2e._config import get_scaleway_config
	from atlas.tests.e2e._scaleway import PROVIDER_NAME, scaleway_client

	config = get_scaleway_config()
	filters = {"provider": PROVIDER_NAME, "status": "Active"}
	if server_title:
		filters["title"] = server_title
	rows = frappe.get_all("Server", filters=filters, fields=["name", "provider_resource_id", "ipv4_address"])
	if not rows:
		raise AssertionError(f"no Active Scaleway server found ({filters}) — run provision_only first")
	server = rows[0]
	server_id = server["provider_resource_id"]
	client = scaleway_client()

	v4 = client.create_flexible_ip(project_id=config["project_id"], is_ipv6=False)
	client.attach_flexible_ip(v4["id"], server_id)
	v6 = client.create_flexible_ip(project_id=config["project_id"], is_ipv6=True)
	client.attach_flexible_ip(v6["id"], server_id)

	print(f"[e2e/scw] kept FIPs attached to {server['name']} ({server['ipv4_address']}):")
	print(f"  v4  id={v4['id']}  addr={v4.get('ip_address')}")
	print(f"  v6  id={v6['id']}  addr={v6.get('ip_address')}")
	print("  drop with: client.detach_flexible_ip(id); client.delete_flexible_ip(id)")


def teardown() -> None:
	"""Delete every Active Scaleway e2e server (and warn on tagged leaks). The
	operator runs this to drop the box `run` left up."""
	from atlas.tests.e2e._scaleway import PROVIDER_NAME

	sweep_old_scaleway_servers()
	rows = frappe.get_all(
		"Server",
		filters={"provider": PROVIDER_NAME},
		fields=["name", "provider_resource_id", "status"],
	)
	if not rows:
		print("[e2e/scw] no Scaleway e2e servers to delete")
		return
	for row in rows:
		if row["provider_resource_id"]:
			cleanup_scaleway_server(row["provider_resource_id"])
			frappe.db.set_value("Server", row["name"], "status", "Archived")
	frappe.db.commit()
