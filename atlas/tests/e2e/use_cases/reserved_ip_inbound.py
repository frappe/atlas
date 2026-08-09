"""Use case: attach a Reserved IP to a VM and prove inbound + egress v4.

This is the host-bound proof of the inbound-v4 primitive (spec/06-networking.md
"IPv4 ingress"). Only a real droplet can show it, so it lives in the e2e suite,
not the unit tests (the rule generation + the attach/detach invariant are
unit-covered in milliseconds; see scripts/lib/atlas/test_reserved_ip_nat.py and
atlas/atlas/doctype/reserved_ip/test_reserved_ip.py).

What it proves, end to end, with a REAL DigitalOcean reserved IP:

- **Inbound DNAT** — from OFF the droplet (the controller, i.e. wherever this
  test runs, over the public v4 internet), reach the reserved IPv4 on the guest's
  sshd `:22` and run `hostname`, asserting it returns *this* guest's
  `atlas-<uuid8>`. That is the production path: external client → DO edge →
  droplet anchor → host PREROUTING DNAT → guest. A host-local curl would skip
  PREROUTING, so the vantage must be off-droplet — the controller is.
- **Egress SNAT** — from inside the guest (hopped in over its v6), curl an
  external v4 echo and assert the observed source is the reserved IP, not the
  host's shared masquerade address (phase-reserved-ip-snat.sh).

It allocates and releases a real reserved IP, so teardown is in a `finally`:
detach (tears down the host NAT + unbinds at the vendor) then release (destroys
the vendor IP). Leaving one attached would strand a billable address.
"""

import subprocess
import time

import frappe

from atlas.tests.e2e._shared import (
	assert_probe,
	ensure_image_on_server,
	ephemeral_private_key,
	ephemeral_public_key,
	phase,
	wait_for_vm_running,
)


def run(reuse: bool = True, keep: bool = True) -> None:
	"""Full path. Same as run_smoke today — the whole use case is host-bound, so
	there is no extra unit-coverable layer to add here (kept for symmetry with the
	other use cases' run/run_smoke split)."""
	run_smoke(reuse=reuse, keep=keep)


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	with phase("reserved-ip-inbound (smoke)", reuse=reuse, keep=keep) as server:
		image = ensure_image_on_server(server.name).name
		vm = _provision_vm(server.name, image)
		reserved = None
		try:
			reserved = _allocate_and_attach(server.name, vm.name)
			reserved_ipv4 = frappe.db.get_value("Reserved IP", reserved, "ip_address")

			# Inbound DNAT: reach the reserved v4 from off the droplet (controller).
			_assert_inbound_reaches_guest(reserved_ipv4, vm.name)

			# Egress SNAT: the guest's v4 egress is stamped with the reserved IP.
			assert_probe(
				server.name,
				"phase-reserved-ip-snat",
				timeout_seconds=180,
				VIRTUAL_MACHINE_IPV6=vm.ipv6_address,
				RESERVED_IPV4=reserved_ipv4,
				SSH_PRIVATE_KEY=ephemeral_private_key(),
			)
		finally:
			_teardown(reserved, vm.name)


def _provision_vm(server_name: str, image: str) -> "frappe.model.document.Document":
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "reserved-ip inbound probe",
			"server": server_name,
			"image": image,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": ephemeral_public_key(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	wait_for_vm_running(vm.name, timeout_seconds=120)
	vm.reload()
	assert vm.status == "Running", vm.status
	return vm


def _allocate_and_attach(server_name: str, vm_name: str) -> str:
	"""Reserve a real DO v4, attach it (vendor assign + host 1:1-NAT Task), and
	return the Reserved IP row name. attach() denormalizes onto the VM, so reload
	a stale handle elsewhere."""
	from atlas.atlas.doctype.reserved_ip import reserved_ip as module

	reserved = module.allocate(server_name)
	frappe.db.commit()
	frappe.get_doc("Reserved IP", reserved).attach(vm_name)
	frappe.db.commit()
	return reserved


def _assert_inbound_reaches_guest(reserved_ipv4: str, vm_name: str) -> None:
	"""From the controller (off the droplet), SSH to the reserved v4 on :22 and
	run `hostname`; assert it is this guest's atlas-<uuid8>. Proves external v4 →
	DNAT → this exact guest. Polls: the DNAT, the DO reserved-IP assignment
	propagating at the edge, and the guest sshd all settle within ~2 min.

	Loud on failure (per the chosen vantage): if the controller genuinely has no
	v4 path to a DO reserved IP, this fails with a clear message rather than
	silently passing."""
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
					f"root@{reserved_ipv4}",
					"hostname",
				],
				capture_output=True,
				text=True,
				timeout=30,
			)
			if result.returncode == 0:
				actual = result.stdout.strip()
				assert actual == expected, (
					f"inbound v4 reached a guest, but hostname={actual!r} want={expected!r} "
					f"(DNAT pointed at the wrong guest?)"
				)
				print(f"[e2e] inbound v4 {reserved_ipv4} -> {actual} OK")
				return
			last_error = (result.stderr or result.stdout).strip()
		except subprocess.TimeoutExpired:
			last_error = "ssh timed out"
		time.sleep(5)
	raise AssertionError(
		f"inbound v4 to {reserved_ipv4}:22 never reached the guest within 150s "
		f"(last error: {last_error!r}). Either the host 1:1-NAT DNAT is wrong, the DO "
		f"reserved IP didn't bind to the droplet, or the controller running this test "
		f"has no outbound IPv4 path to a DO reserved IP."
	)


def _controller_key_path() -> str:
	"""Write the ephemeral private key to a 0600 temp file the controller's ssh
	can use, and return its path. (The host-side probes get the key as a Task
	variable; the controller-side inbound probe needs it on local disk.)"""
	import os
	import tempfile

	directory = os.path.expanduser("~/.cache/atlas-e2e")
	os.makedirs(directory, exist_ok=True)
	path = os.path.join(directory, "inbound-probe.key")
	with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
		handle.write(ephemeral_private_key())
	return path


def _teardown(reserved: str | None, vm_name: str) -> None:
	"""Always: detach + release the (billable) reserved IP, then terminate the VM.
	Each step guarded so one failure doesn't strand the others."""
	if reserved and frappe.db.exists("Reserved IP", reserved):
		try:
			doc = frappe.get_doc("Reserved IP", reserved)
			if doc.virtual_machine:
				doc.detach()
			doc.release()
			frappe.db.commit()
		except Exception:
			import traceback

			print(f"[e2e] WARNING: reserved IP {reserved} teardown failed — release it by hand:")
			traceback.print_exc()
	if frappe.db.exists("Virtual Machine", vm_name):
		vm = frappe.get_doc("Virtual Machine", vm_name)
		if vm.status not in ("Terminated",):
			vm.terminate()
			frappe.db.commit()
