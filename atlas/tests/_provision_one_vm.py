"""Operator one-shot: provision ONE Virtual Machine on a given Server via the
real API, drive it to Running inline, and KEEP it.

This is the "Check with One VM provisioning from the API" verification: it
exercises the real provision path (image sync -> VM insert -> provision-vm.py
on the host) end-to-end against a live Server.

VM auto-provision normally runs in the `long` worker queue (after_insert ->
auto_provision). We call `provision()` INLINE so the result is deterministic
in one process; the enqueued auto_provision job becomes a no-op (it short
circuits unless status == Pending).

Run:
  bench --site atlas.tests.local execute atlas.tests._provision_one_vm.run --kwargs "{'server': '<server-name>'}"
"""

import time

import frappe

from atlas.tests.e2e._config import ephemeral_public_key
from atlas.tests.e2e._image import ensure_image_on_server


def run(server: str, keep: bool = True):
	server_doc = frappe.get_doc("Server", server)
	if server_doc.status != "Active":
		frappe.throw(f"Server {server!r} is {server_doc.status!r}, need Active")
	print(f"[vm] target server={server!r} ipv4={server_doc.ipv4_address!r} status=Active")

	# Prereq: the base image must be present on the host. sync-image.py is
	# idempotent and short-circuits if the rootfs is already there.
	print("[vm] ensuring base image is synced to the server (idempotent)...")
	image = ensure_image_on_server(server)
	print(f"[vm] image ready: {image.name!r}")

	start = time.monotonic()
	vm = frappe.get_doc({
		"doctype": "Virtual Machine",
		"title": f"ui-capacity-check-{int(time.time())}",
		"server": server,
		"image": image.name,
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 4,
		"ssh_public_key": ephemeral_public_key(),
	}).insert(ignore_permissions=True)
	frappe.db.commit()
	print(f"[vm] inserted Virtual Machine {vm.name!r} status={vm.status!r}")

	# Drive provision inline (the API method the operator/worker calls). This
	# runs provision-vm.py on the host: thin-LV snapshot, jailed Firecracker,
	# routed tap, guest identity. Returns the on-host Task name.
	print("[vm] calling provision() inline (runs provision-vm.py on host)...")
	task_name = vm.provision()
	vm.reload()
	elapsed = int(time.monotonic() - start)
	task = frappe.get_doc("Task", task_name)
	print(f"[vm] provision Task {task_name!r} status={task.status!r}")
	print(
		f"[vm] RESULT name={vm.name} title={vm.title!r} status={vm.status!r} "
		f"ipv6={vm.ipv6_address!r} tap={vm.tap_device!r} mac={vm.mac_address!r} "
		f"ssh={vm.ssh_command!r} in {elapsed}s"
	)

	if vm.status != "Running":
		frappe.throw(f"VM ended {vm.status!r}, expected Running — Task stderr: {(task.stderr or '')[:500]}")

	if keep:
		print("[vm] One-VM provisioning via the API: OK. VM is Running and KEPT.")
	else:
		vm.terminate()
		print("[vm] One-VM provisioning via the API: OK. VM terminated (keep=False).")
	return vm.name
