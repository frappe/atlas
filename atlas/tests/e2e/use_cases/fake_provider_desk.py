"""Use case: every operator-visible Desk button, driven through the HTTP layer
the desk uses — against the **Fake provider**, no real cloud.

This is the Fake-provider analogue of `desk_buttons`. That module proves the
desk seam against a *real* droplet (and so is gated on a live DO account); this
one proves the same seam with `provider_type = Fake`, so it runs anywhere
`developer_mode` is on — e.g. `fake.local` — with no droplet, no SSH, in
seconds.

It drives every form button through the same wrappers the desk hits:

- Controller methods (`Provision`, `Start`, `Stop`, …) via
  `frappe.handler.run_doc_method`, the endpoint `frm.call(...)` posts to.
- Module-function buttons (Reserved IP **Allocate** / **Discover**, which the
  Server form calls as `frappe.call({method: "…reserved_ip.allocate"})`) via
  `frappe.handler.execute_cmd`, the endpoint that path posts to.

Both with the exact argument shapes the desk sends (dialog fields ship strings).

It is invoked directly (like `tls_issuance` / `self_serve_site`), not folded into
`run_all_smoke`, because it needs no shared droplet and is self-contained: it
points Atlas Settings at the Fake provider + creates its own server + image + VMs
and tears them all down in a `finally`. Run it:

    bench --site fake.local execute atlas.tests.e2e.use_cases.fake_provider_desk.run
"""

from __future__ import annotations

import json
import time

import frappe

from atlas.atlas.providers.fake import FAKE_PROVIDER_TYPE
from atlas.tests.e2e._config import ephemeral_public_key
from atlas.tests.e2e._tasks import expect_validation_error
from atlas.tests.e2e.use_cases.desk_buttons import _call_button, _fake_post_request

# A self-contained namespace so a run never collides with the demo fleet.
SERVER_TITLE = "fake-e2e-server"
IMAGE_NAME = "fake-e2e-image"


def run() -> None:
	"""Full Desk-button pass against the Fake provider. Self-contained.

	`frappe.enqueue` is patched out for the whole pass: every VM's `after_insert`
	would otherwise enqueue `auto_provision` onto the live worker, which would race
	this test's own explicit `provision()` calls for the same row (lock contention,
	double provision). The worker→auto_provision path has its own unit regression
	(`test_auto_provision_entrypoint_succeeds_without_ssh_key`); here we drive the
	desk methods synchronously and deterministically."""
	from unittest.mock import patch

	_require_developer_mode()
	# Save the site's active provider type — setup repoints it at the Fake vendor;
	# restore it so we never leave Atlas Settings pointed at a dev-only provider.
	previous_provider_type = frappe.db.get_single_value("Atlas Settings", "provider_type")
	_cleanup()  # idempotent: clear any leftovers from a prior aborted run
	with patch("frappe.enqueue"):
		try:
			_setup_provider()
			server = _provision_server_via_desk()
			image = _setup_image(server.name)
			_check_server_buttons(server)
			_check_image_buttons(server.name, image.name)
			_check_reserved_ip_buttons(server)
			_check_virtual_machine_lifecycle(server.name, image.name)
			_check_fault_injection(server.name, image.name)
			print("[fake-desk] all Desk buttons OK")
		finally:
			_cleanup()
			if previous_provider_type:
				frappe.db.set_single_value(
					"Atlas Settings", "provider_type", previous_provider_type, update_modified=False
				)
				frappe.db.commit()


# Alias so the module matches the run_smoke/run convention of its siblings. There
# are no host facts to trim here — every check is the desk HTTP layer — so smoke
# equals the full run.
run_smoke = run


def _require_developer_mode() -> None:
	if not frappe.conf.developer_mode:
		frappe.throw("fake_provider_desk e2e needs developer_mode (the Fake provider is dev-only)")


# ----- setup ---------------------------------------------------------------


def _setup_provider():
	"""Activate the Fake provider on Atlas Settings and seed its catalog via the
	desk's Refresh Catalog button (refresh_catalog)."""
	frappe.db.set_single_value("Atlas Settings", "provider_type", FAKE_PROVIDER_TYPE, update_modified=False)
	if not frappe.db.get_single_value("Atlas Settings", "ssh_private_key_path"):
		frappe.db.set_single_value(
			"Atlas Settings", "ssh_private_key_path", _throwaway_key(), update_modified=False
		)
	frappe.db.commit()

	# Authenticate button: returns AuthResult-as-dict, ok=True for Fake.
	result = _call_button("Atlas Settings", "Atlas Settings", "authenticate")
	assert result and result.get("ok"), result

	# Refresh Catalog button: upserts Provider Size / Provider Image rows.
	counts = _call_button("Atlas Settings", "Atlas Settings", "refresh_catalog")
	assert counts and (counts["inserted"] or counts["updated"]), counts


def _throwaway_key() -> str:
	"""A real 0600 key file so connection_for_server's eager read never throws,
	even if a code path ever bypasses the fake guard. Never used to connect."""
	import os

	path = frappe.get_site_path("private", "files", "fake-e2e-key.pem")
	if not os.path.exists(path):
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, "w") as handle:
			handle.write("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n")
		os.chmod(path, 0o600)
	return os.path.abspath(path)


def _provision_server_via_desk():
	"""Provision Server button: the dialog posts title (+ optional size/image) as
	strings. The worker (faked) drives Pending -> Active inline here."""
	from atlas.atlas.providers.worker import finish_provisioning

	server_name = _call_button("Atlas Settings", "Atlas Settings", "provision_server", title=SERVER_TITLE)
	assert server_name, "provision_server returned no name"
	frappe.db.commit()

	# Duplicate-name negative: same title throws, no second row.
	with expect_validation_error("already exists"):
		_call_button("Atlas Settings", "Atlas Settings", "provision_server", title=SERVER_TITLE)

	# Run the worker inline (normally enqueued) so the rest of the pass has an
	# Active server. This is the real worker path — faked SSH, real state machine.
	finish_provisioning(server_name)
	server = frappe.get_doc("Server", server_name)
	assert server.status == "Active", server.status
	assert server.ipv4_address and server.firecracker_version, (
		server.ipv4_address,
		server.firecracker_version,
	)
	return server


def _setup_image(server_name: str):
	if not frappe.db.exists("Virtual Machine Image", IMAGE_NAME):
		frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				"image_name": IMAGE_NAME,
				"title": "Fake e2e image",
				"is_active": 1,
				"default_disk_gigabytes": 4,
				"kernel_url": "https://images.invalid/vmlinux",
				"kernel_filename": "vmlinux",
				"kernel_sha256": "0" * 64,
				"rootfs_url": "https://images.invalid/rootfs.squashfs",
				"rootfs_filename": "fake-e2e.ext4",
				"rootfs_sha256": "0" * 64,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()
	return frappe.get_doc("Virtual Machine Image", IMAGE_NAME)


# ----- Server --------------------------------------------------------------


def _check_server_buttons(server) -> None:
	"""Bootstrap, Run Task dialog (+ negatives) — all faked, all via run_doc_method."""
	task_name = _call_button("Server", server.name, "bootstrap")
	task = frappe.get_doc("Task", task_name)
	assert task.status == "Success", task.stderr

	# Run Task dialog happy path: Code field posts variables as a JSON string.
	task_name = _call_button(
		"Server",
		server.name,
		"run_task_dialog",
		script="bootstrap-server.py",
		variables=json.dumps({"FIRECRACKER_VERSION": "v1.16.0", "ARCHITECTURE": "x86_64"}),
	)
	assert frappe.get_doc("Task", task_name).status == "Success"

	# Malformed JSON in the Code field surfaces as a clean ValidationError.
	with expect_validation_error("must be valid json"):
		_call_button(
			"Server", server.name, "run_task_dialog", script="bootstrap-server.py", variables="{nope"
		)

	# Unknown script is rejected.
	with expect_validation_error("unknown script"):
		_call_button("Server", server.name, "run_task_dialog", script="not-real.sh", variables="")


# ----- Virtual Machine Image ----------------------------------------------


def _check_image_buttons(server_name: str, image_name: str) -> None:
	"""Sync to Server (Link field posts a string) + Sync to All Servers.

	`sync_to_server` is async — it inserts a Pending Task and enqueues
	`execute_task` (the worker runs it). The button correctly returns the Pending
	row; we then drive `execute_task` synchronously (the image_sync use case's
	pattern) to prove the fake sync runs through to Success with no SSH."""
	from atlas.atlas.ssh import execute_task

	task_name = _call_button("Virtual Machine Image", image_name, "sync_to_server", server_name=server_name)
	task = frappe.get_doc("Task", task_name)
	assert task.script == "sync-image.py", task.script
	assert task.status in ("Pending", "Running", "Success"), task.status
	execute_task(task_name)  # the enqueued job, run inline (faked, no SSH)
	task.reload()
	assert task.status == "Success", task.stderr

	# Sync to All Servers fans out one Task per Active server. On a site that also
	# has the demo fleet, that includes a Self-Managed host — whose Task correctly
	# routes to REAL SSH (per-Server routing). Only drive the Fake-backed ones here.
	from atlas.atlas.providers.fake_tasks import is_fake_server

	tasks = _call_button("Virtual Machine Image", image_name, "sync_to_all_servers")
	assert isinstance(tasks, list) and tasks, tasks
	fake_tasks = [name for name in tasks if is_fake_server(frappe.db.get_value("Task", name, "server"))]
	assert fake_tasks, "expected at least one sync Task on a Fake server"
	for name in fake_tasks:
		execute_task(name)
		assert frappe.get_doc("Task", name).status == "Success"


# ----- Reserved IP ---------------------------------------------------------


def _check_reserved_ip_buttons(server) -> None:
	"""Allocate / Discover (Server-form module-function buttons) and
	Attach / Detach / Release (Reserved IP controller-method buttons)."""
	# Allocate Reserved IP: the Server form calls a module function, not a
	# controller method — exercise the execute_cmd path the desk actually uses.
	ip_name = _call_command("atlas.atlas.doctype.reserved_ip.reserved_ip.allocate", server=server.name)
	assert ip_name, "allocate returned no name"
	frappe.db.commit()
	ip = frappe.get_doc("Reserved IP", ip_name)
	assert ip.status == "Allocated", ip.status

	# Discover Reserved IPs: Fake lists none, so it imports nothing (returns []).
	discovered = _call_command("atlas.atlas.doctype.reserved_ip.reserved_ip.discover", server=server.name)
	assert discovered == [], discovered

	# Attach needs a VM on the same server. Make a quick Running one.
	vm = _make_running_vm(server.name, "fake-e2e-rip-vm")
	_call_button("Reserved IP", ip_name, "attach", virtual_machine=vm.name)
	ip.reload()
	assert ip.status == "Attached" and ip.virtual_machine == vm.name, (ip.status, ip.virtual_machine)
	assert frappe.db.get_value("Virtual Machine", vm.name, "public_ipv4") == ip.ip_address

	# Detach returns it to the pool.
	_call_button("Reserved IP", ip_name, "detach")
	ip.reload()
	assert ip.status == "Allocated" and not ip.virtual_machine, (ip.status, ip.virtual_machine)

	# Release destroys the vendor IP (no-op for Fake) and deletes the row.
	_call_button("Reserved IP", ip_name, "release")
	assert not frappe.db.exists("Reserved IP", ip_name)

	frappe.get_doc("Virtual Machine", vm.name).terminate()


# ----- Virtual Machine lifecycle ------------------------------------------


def _check_virtual_machine_lifecycle(server_name: str, image_name: str) -> None:
	"""Every VM button through run_doc_method, mirroring virtual_machine.js,
	with the wrong-state negatives inline. No SSH the whole way."""
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "fake-e2e-lifecycle",
			"server": server_name,
			"image": image_name,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": ephemeral_public_key(),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	# Provision (the form's retry button; also the auto_provision target).
	_call_button("Virtual Machine", vm.name, "provision")
	vm.reload()
	assert vm.status == "Running", vm.status

	# Stop / Start.
	_call_button("Virtual Machine", vm.name, "stop")
	vm.reload()
	assert vm.status == "Stopped", vm.status
	_call_button("Virtual Machine", vm.name, "start")
	vm.reload()
	assert vm.status == "Running", vm.status

	# Stop (memory snapshot): the desk posts {"memory_snapshot": true}.
	_call_button("Virtual Machine", vm.name, "stop", memory_snapshot=True)
	vm.reload()
	assert vm.status == "Stopped" and vm.has_memory_snapshot, (vm.status, vm.has_memory_snapshot)
	_call_button("Virtual Machine", vm.name, "start")
	vm.reload()
	assert vm.status == "Running" and not vm.has_memory_snapshot, (vm.status, vm.has_memory_snapshot)

	# Pause / Resume + wrong-state negatives.
	with expect_validation_error("cannot resume"):
		_call_button("Virtual Machine", vm.name, "resume")
	_call_button("Virtual Machine", vm.name, "pause")
	vm.reload()
	assert vm.status == "Paused", vm.status
	_call_button("Virtual Machine", vm.name, "resume")
	vm.reload()
	assert vm.status == "Running", vm.status

	# Restart returns {stop_task, start_task}.
	result = _call_button("Virtual Machine", vm.name, "restart")
	assert result and result.get("stop_task") and result.get("start_task"), result

	_check_snapshot_family(vm)

	# Terminate (from Stopped) + already-terminated guard.
	_call_button("Virtual Machine", vm.name, "terminate")
	vm.reload()
	assert vm.status == "Terminated", vm.status
	with expect_validation_error("already terminated"):
		_call_button("Virtual Machine", vm.name, "terminate")


def _check_snapshot_family(vm) -> None:
	"""Snapshot / Restore / Rebuild / Resize / Clone + dialog arg shapes and
	negatives. Enters Running; leaves the VM Stopped."""
	# Snapshot/resize rejected while Running.
	with expect_validation_error("stop the vm before snapshotting"):
		_call_button("Virtual Machine", vm.name, "snapshot", title="too early")
	with expect_validation_error("stop the vm before resizing"):
		_call_button("Virtual Machine", vm.name, "resize", vcpus=2)

	_call_button("Virtual Machine", vm.name, "stop")
	vm.reload()
	assert vm.status == "Stopped", vm.status

	# Snapshot (title posts as a Data string).
	snapshot_name = _call_button("Virtual Machine", vm.name, "snapshot", title="fake snap")
	snapshot = frappe.get_doc("Virtual Machine Snapshot", snapshot_name)
	assert snapshot.status == "Available", snapshot.status

	# Restore onto its own VM (Snapshot form button).
	assert _call_button("Virtual Machine Snapshot", snapshot_name, "restore_to_vm")

	# Rebuild from image (+ unknown-source negative).
	assert _call_button("Virtual Machine", vm.name, "rebuild", source_type="image", source=vm.image)
	with expect_validation_error("unknown rebuild source_type"):
		_call_button("Virtual Machine", vm.name, "rebuild", source_type="banana")

	# Resize (Int fields post as strings) + shrink negative.
	_call_button("Virtual Machine", vm.name, "resize", vcpus="2", memory_megabytes="1024", disk_gigabytes="6")
	vm.reload()
	assert vm.vcpus == 2 and vm.disk_gigabytes == 6, (vm.vcpus, vm.disk_gigabytes)
	with expect_validation_error("can only grow"):
		_call_button("Virtual Machine", vm.name, "resize", disk_gigabytes="4")

	# Clone into a new VM (Snapshot form button); provision the clone (faked).
	clone_name = _call_button(
		"Virtual Machine Snapshot",
		snapshot_name,
		"clone_to_new_vm",
		title="fake clone",
		ssh_public_key=ephemeral_public_key(),
	)
	assert clone_name and clone_name != vm.name, clone_name
	frappe.db.commit()
	clone = frappe.get_doc("Virtual Machine", clone_name)
	clone.provision()
	clone.reload()
	assert clone.status == "Running", clone.status
	clone.terminate()

	# Delete the snapshot row.
	frappe.delete_doc("Virtual Machine Snapshot", snapshot_name, ignore_permissions=True)
	assert not frappe.db.exists("Virtual Machine Snapshot", snapshot_name)


# ----- fault injection -----------------------------------------------------


def _check_fault_injection(server_name: str, image_name: str) -> None:
	"""The Fake provider's "fake a failed action": Atlas Settings with
	fail_scripts="provision-vm.py" makes Provision fail through the desk exactly
	like a real failure — the VM lands Failed and the Task is Failure."""
	frappe.db.set_value("Atlas Settings", "Atlas Settings", "fail_scripts", "provision-vm.py")
	try:
		vm = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"title": "fake-e2e-willfail",
				"server": server_name,
				"image": image_name,
				"vcpus": 1,
				"memory_megabytes": 512,
				"disk_gigabytes": 4,
				"ssh_public_key": ephemeral_public_key(),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		with expect_validation_error("fail"):
			_call_button("Virtual Machine", vm.name, "provision")
		vm.reload()
		assert vm.status == "Failed", vm.status
		task = frappe.get_last_doc("Task", filters={"virtual_machine": vm.name})
		assert task.status == "Failure", task.status
		vm.terminate()
	finally:
		frappe.db.set_value("Atlas Settings", "Atlas Settings", "fail_scripts", "")
		frappe.db.commit()


# ----- helpers -------------------------------------------------------------


def _call_command(method: str, **args) -> object:
	"""Invoke a whitelisted MODULE FUNCTION the way the desk's
	`frappe.call({method, args})` does — through `frappe.handler.execute_cmd`,
	the dispatcher that path posts to. (Distinct from `_call_button`, which is for
	controller methods via `run_doc_method`.)

	`execute_cmd` reads args from `frappe.form_dict` and RETURNS the value
	(the outer `handle()` is what copies it into `frappe.response["message"]`),
	so we capture the return directly."""
	from frappe.handler import execute_cmd

	previous_form_dict = frappe.local.form_dict
	frappe.local.form_dict = frappe._dict(args)
	try:
		with _fake_post_request():
			return execute_cmd(method)
	finally:
		frappe.local.form_dict = previous_form_dict


def _make_running_vm(server_name: str, title: str):
	from unittest.mock import patch

	with patch("frappe.enqueue"):
		vm = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"title": title,
				"server": server_name,
				"image": IMAGE_NAME,
				"vcpus": 1,
				"memory_megabytes": 512,
				"disk_gigabytes": 4,
				"ssh_public_key": ephemeral_public_key(),
			}
		).insert(ignore_permissions=True)
	vm.provision()
	vm.reload()
	return vm


def _cleanup() -> None:
	"""Remove everything this module creates, in dependency order. Idempotent."""
	servers = frappe.get_all(
		"Server", filters={"provider_type": FAKE_PROVIDER_TYPE, "title": SERVER_TITLE}, pluck="name"
	)
	for ip in frappe.get_all("Reserved IP", filters={"server": ["in", servers or ["x"]]}, pluck="name"):
		row = frappe.get_doc("Reserved IP", ip)
		if row.virtual_machine:
			row.detach()
		frappe.delete_doc("Reserved IP", ip, force=True, ignore_permissions=True)
	for doctype in ("Task", "Virtual Machine Snapshot", "Virtual Machine"):
		for name in frappe.get_all(doctype, filters={"server": ["in", servers or ["x"]]}, pluck="name"):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
	for name in servers:
		frappe.delete_doc("Server", name, force=True, ignore_permissions=True)
	if frappe.db.exists("Virtual Machine Image", IMAGE_NAME):
		frappe.delete_doc("Virtual Machine Image", IMAGE_NAME, force=True, ignore_permissions=True)
	frappe.db.commit()
