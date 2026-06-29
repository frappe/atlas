"""One-off live verification of the verb / console-script cutover.

Provisions a FRESH droplet, bootstraps it (the controller SSHes scripts/install.sh
after the upload, which creates the Atlas venv + `atlas` console script; the
bootstrap Task then runs as `atlas bootstrap-server` on that venv — no carve-out),
then proves on the real host that:

  1. Server.cli_ready == 1 after a successful bootstrap.
  2. The bootstrap Task is recorded with the VERB `bootstrap-server` (not
     `bootstrap-server.py`).
  3. The `atlas` console script is on PATH and dispatches verbs
     (`atlas --help`, `atlas start-vm --help`) — the exact entry the runner uses.
  4. The console script's interpreter is the Atlas venv python (uv pip install),
     and bootstrap-server ITSELF ran under that venv python — NOT the host's
     /usr/bin/python3. (This INVERTS the pre-install.sh check: there is no longer a
     carve-out running bootstrap on stock python3.)

Run:  bench --site e2e.local execute atlas.tests.e2e._verify_verbs.run
Tears the droplet down at the end unless keep=True.
"""

from __future__ import annotations

import time

import frappe

from atlas.atlas.provisioning import region_server_title
from atlas.tests.e2e._config import get_client
from atlas.tests.e2e._droplets import cleanup_droplet, ensure_e2e_provider, sweep_old_droplets


def _ssh(server_doc, command: str, timeout: int = 30) -> tuple[str, str, int]:
	from atlas.atlas._ssh.transport import run_ssh, ssh_key_file
	from atlas.atlas.ssh import connection_for_server

	connection = connection_for_server(server_doc)
	with ssh_key_file(connection.ssh_private_key) as key_path:
		return run_ssh(connection, key_path, command, timeout_seconds=timeout)


def run(keep: bool = False) -> None:
	start = time.monotonic()

	# Neutralize background enqueue for this run. This bench's worker pool runs
	# stale pre-rename bytecode and would grab any enqueued job (auto_provision,
	# execute_task, finish_provisioning) and race our inline drives — or run the
	# OLD catalog. We drive every step synchronously below, so enqueue must no-op.
	original_enqueue = frappe.enqueue

	def _no_enqueue(*_a, **_k):
		return None

	frappe.enqueue = _no_enqueue
	try:
		_run(keep, start)
	finally:
		frappe.enqueue = original_enqueue


def _run(keep: bool, start: float) -> None:
	client = get_client()
	sweep_old_droplets(client)
	ensure_e2e_provider()

	title = region_server_title("verify-verbs")
	server = None
	try:
		print(f"[verify] provisioning fresh droplet ({title}) …")
		server_name = frappe.get_single("Atlas Settings").provision_server(title)

		# provision_server enqueues finish_provisioning on the `long` RQ queue. This
		# bench has no worker running, so run it INLINE (the SSH-wait + bootstrap)
		# rather than waiting on a job that will never fire — the same recovery the
		# operator uses for a lost finish_provisioning job.
		from atlas.atlas.providers import worker

		print("[verify] running finish_provisioning inline (SSH-wait + bootstrap) …")
		worker.finish_provisioning(server_name)

		frappe.db.rollback()
		server = frappe.get_doc("Server", server_name)
		assert server.status == "Active", f"expected Active, got {server.status}"
		print(f"[verify] server Active after {int(time.monotonic() - start)}s: {server.ipv4_address}")

		# (1) cli_ready persisted at bootstrap.
		assert server.cli_ready == 1, f"cli_ready not set: {server.cli_ready!r}"
		print("[verify] OK (1) Server.cli_ready == 1")

		# (2) the bootstrap Task is recorded with the verb.
		bootstrap_tasks = frappe.get_all(
			"Task",
			filters={"server": server_name, "script": "bootstrap-server", "status": "Success"},
		)
		assert bootstrap_tasks, "no Success Task with script == 'bootstrap-server' (verb) found"
		# And no legacy '.py' row was written for this fresh server.
		legacy = frappe.get_all("Task", filters={"server": server_name, "script": "bootstrap-server.py"})
		assert not legacy, f"unexpected legacy .py Task rows: {legacy}"
		print("[verify] OK (2) bootstrap Task recorded as verb 'bootstrap-server'")

		# (3) the atlas console script dispatches verbs on the real host.
		out, err, code = _ssh(server, "which atlas && atlas --help")
		assert code == 0, f"`atlas --help` failed: {err or out}"
		assert "start-vm" in out and "provision-vm" in out, f"verbs missing from --help:\n{out}"
		print("[verify] OK (3a) `atlas --help` lists verbs (start-vm, provision-vm)")
		out, err, code = _ssh(server, "atlas start-vm --help")
		assert code == 0, f"`atlas start-vm --help` failed: {err or out}"
		assert "--virtual-machine-name" in out, f"start-vm flags missing:\n{out}"
		print("[verify] OK (3b) `atlas start-vm --help` dispatches to the typed entry")

		# (4) console script runs on the venv python — and so does bootstrap-server
		# itself (NO carve-out: install.sh created the venv before the bootstrap Task,
		# so bootstrap ran as `atlas bootstrap-server` on the venv, not host python3).
		out, _e, code = _ssh(server, "/var/lib/atlas/venv/bin/python --version")
		assert code == 0, f"venv python probe failed: {out}"
		venv_version = out.strip()
		print(f"[verify] OK (4a) venv python present: {venv_version}")
		out, _e, code = _ssh(server, "head -1 $(which atlas)")
		assert "/var/lib/atlas/venv" in out, f"atlas shebang not the venv:\n{out}"
		print(f"[verify] OK (4b) `atlas` shebang is the venv python: {out.strip()}")
		# (4c) THE INVERSION: bootstrap ran under the venv python, not stock python3.
		# The bootstrap Task emitted python_version read live from /var/lib/atlas/venv/
		# bin/python; the on-host bootstrap.json carries the same value. Assert it is
		# the venv interpreter's version — proof the carve-out is gone.
		task = frappe.get_doc("Task", bootstrap_tasks[0]["name"])
		assert "ATLAS_RESULT=" in (task.stdout or ""), "bootstrap Task has no ATLAS_RESULT line"
		from atlas.atlas.task_results import parse_result

		python_version = parse_result(task.stdout)["python_version"]
		assert python_version in venv_version or venv_version in python_version, (
			f"bootstrap python_version {python_version!r} != venv python {venv_version!r}; "
			"bootstrap did not run under the venv interpreter"
		)
		out, _e, _code = _ssh(server, "cat /var/lib/atlas/bootstrap.json")
		assert "3.14" in out and python_version.split()[-1] in out, (
			f"bootstrap.json python_version off:\n{out}"
		)
		print(f"[verify] OK (4c) bootstrap-server ran under the venv python ({python_version}), no carve-out")

		# (5) END-TO-END python lifecycle verbs as real Tasks. Sync an image, then
		# drive a VM through provision() + stop() + start() — each calls
		# run_task(script="<verb>") which executes `atlas <verb>` on the host. Assert
		# every Task row is the verb and Succeeded.
		_verify_vm_lifecycle(server)

		print(f"[verify] ALL CHECKS PASSED in {int(time.monotonic() - start)}s")
	finally:
		if server is not None and server.provider_resource_id and not keep:
			print("[verify] tearing down droplet …")
			try:
				cleanup_droplet(client, int(server.provider_resource_id))
			except Exception as exc:
				print(f"[verify] teardown warning: {exc}")


def _last_task(vm_name: str, verb: str):
	rows = frappe.get_all(
		"Task",
		filters={"virtual_machine": vm_name, "script": verb},
		fields=["name", "script", "status", "stderr"],
		order_by="creation desc",
		limit=1,
	)
	assert rows, f"no Task row for verb {verb!r} on VM {vm_name}"
	return rows[0]


def _verify_vm_lifecycle(server) -> None:
	"""Sync an image, then run a VM through provision/stop/start INLINE so each
	`atlas <verb>` Task executes on the real host. Asserts each Task is the verb
	and Succeeded — the end-to-end proof the runner's console-script path works."""
	from atlas.atlas.ssh import run_task
	from atlas.tests.e2e._config import ephemeral_public_key
	from atlas.tests.e2e._image import ensure_default_image_row

	# Sync the image with a SYNCHRONOUS run_task — NOT sync_to_server (which
	# enqueues execute_task on the `long` queue, where this bench's worker pool,
	# running stale pre-rename bytecode, would race us). run_task creates + runs +
	# finalizes the Task in-process, executing `atlas sync-image` on the host.
	image_doc = ensure_default_image_row()
	sync_task = run_task(
		server=server.name,
		script="sync-image",
		variables={
			"IMAGE_NAME": image_doc.image_name,
			"KERNEL_URL": image_doc.kernel_url,
			"KERNEL_FILENAME": image_doc.kernel_filename,
			"KERNEL_SHA256": image_doc.kernel_sha256,
			"ROOTFS_URL": image_doc.rootfs_url,
			"ROOTFS_FILENAME": image_doc.rootfs_filename,
			"ROOTFS_SHA256": image_doc.rootfs_sha256,
			"DEFAULT_DISK_GB": str(image_doc.default_disk_gigabytes),
			"GUEST_NETWORK_UNIT": "/tmp/atlas/atlas-network.service",
		},
		timeout_seconds=900,
	)
	frappe.db.commit()
	assert sync_task.status == "Success", f"sync-image failed: {(sync_task.stderr or '')[:500]}"
	assert sync_task.script == "sync-image", sync_task.script
	print(f"[verify] OK (5·image) `atlas sync-image` Task Succeeded; image {image_doc.name} on host")

	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "verify-verbs-vm",
			"server": server.name,
			"image": image_doc.name,
			"vcpus": 1,
			"cpu_max_cores": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": image_doc.default_disk_gigabytes,
			"ssh_public_key": ephemeral_public_key(),
			"status": "Pending",
		}
	)
	# after_insert enqueues auto_provision on the `long` queue; with no worker
	# running it never fires, so the VM stays Pending and we drive provision()
	# inline below — each lifecycle method runs `atlas <verb>` synchronously.
	vm.insert(ignore_permissions=True)
	frappe.db.commit()
	try:
		# provision -> `atlas provision-vm` (run_task is synchronous + commits).
		vm.provision()
		frappe.db.commit()
		task = _last_task(vm.name, "provision-vm")
		assert task["status"] == "Success", f"provision-vm failed: {task['stderr']}"
		print("[verify] OK (5a) `atlas provision-vm` Task Succeeded on the host")

		# stop -> `atlas stop-vm` (or snapshot-stop-vm).
		vm.reload()
		vm.stop()
		frappe.db.commit()
		stop = frappe.get_all(
			"Task",
			filters={"virtual_machine": vm.name, "script": ["in", ["stop-vm", "snapshot-stop-vm"]]},
			fields=["script", "status", "stderr"],
			order_by="creation desc",
			limit=1,
		)
		assert stop and stop[0]["status"] == "Success", f"stop failed: {stop}"
		print(f"[verify] OK (5b) `atlas {stop[0]['script']}` Task Succeeded on the host")

		# start -> `atlas start-vm`.
		vm.reload()
		vm.start()
		frappe.db.commit()
		started = _last_task(vm.name, "start-vm")
		assert started["status"] == "Success", f"start-vm failed: {started['stderr']}"
		print("[verify] OK (5c) `atlas start-vm` Task Succeeded on the host")
	finally:
		try:
			vm.reload()
			vm.terminate()
			frappe.db.commit()
			term = _last_task(vm.name, "terminate-vm")
			print(f"[verify] cleanup: `atlas terminate-vm` -> {term['status']}")
		except Exception as exc:
			print(f"[verify] VM teardown warning: {exc}")
