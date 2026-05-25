"""Phase 6 e2e: exercise the VM lifecycle (start/stop/restart/delete)."""

import os
import subprocess
import time
import traceback

import frappe

from atlas.atlas.ssh import run_task_on_server
from atlas.tests.e2e._shared import (
	cleanup_droplet,
	ensure_bootstrapped_server,
	ensure_image_on_server,
	sweep_old_droplets,
)


def run(reuse: bool = True, keep: bool = True) -> None:
	start_clock = time.monotonic()
	server, client, created_now = ensure_bootstrapped_server(reuse=reuse, keep=keep)
	sweep_old_droplets(client)
	image_doc = ensure_image_on_server(server.name)
	image = image_doc.name

	public_key = _ephemeral_public_key()

	vm = frappe.get_doc({
		"doctype": "Virtual Machine",
		"description": "phase 6 e2e",
		"server": server.name,
		"image": image,
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 4,
		"ssh_public_key": public_key,
	}).insert(ignore_permissions=True)

	try:
		vm.provision()
		vm.reload()
		assert vm.status == "Running", vm.status
		first_started = vm.last_started
		_assert_is_active(server.name, vm.name)

		# Stop
		vm.stop()
		vm.reload()
		assert vm.status == "Stopped", vm.status
		assert vm.last_stopped, "last_stopped should be set"
		_assert_is_inactive(server.name, vm.name)

		# Start
		time.sleep(1)  # advance clock for last_started comparison
		vm.start()
		vm.reload()
		assert vm.status == "Running", vm.status
		assert vm.last_started > first_started, (
			f"last_started did not advance: {first_started} -> {vm.last_started}"
		)
		_assert_is_active(server.name, vm.name)

		# Restart (Running -> Running, two tasks)
		before_stop = vm.last_stopped
		before_start = vm.last_started
		time.sleep(1)
		result = vm.restart()
		assert result["stop_task"] and result["start_task"], result
		vm.reload()
		assert vm.status == "Running", vm.status
		assert vm.last_stopped > before_stop, "last_stopped did not advance on restart"
		assert vm.last_started > before_start, "last_started did not advance on restart"
		_assert_is_active(server.name, vm.name)

		# Delete
		tap_device = vm.tap_device
		vm.delete_vm()
		vm.reload()
		assert vm.status == "Archived", vm.status
		_assert_gone(server.name, vm.name, tap_device)

		# Delete again -> raises
		raised = False
		try:
			vm.delete_vm()
		except frappe.ValidationError:
			raised = True
		assert raised, "second delete should raise"
	except Exception:
		elapsed = time.monotonic() - start_clock
		print(f"phase-6: FAIL in {elapsed:.0f}s")
		traceback.print_exc()
		raise
	finally:
		if created_now and not keep and server.provider_resource_id:
			cleanup_droplet(client, int(server.provider_resource_id))

	elapsed = time.monotonic() - start_clock
	print(f"phase-6: OK in {elapsed:.0f}s")


def _ephemeral_public_key() -> str:
	directory = "/tmp/atlas-e2e-keys"
	os.makedirs(directory, exist_ok=True)
	key_path = f"{directory}/id"
	if not os.path.exists(key_path):
		subprocess.run(
			["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path],
			check=True,
		)
	os.chmod(key_path, 0o600)
	return open(f"{key_path}.pub").read().strip()


def _assert_is_active(server_name: str, vm_name: str) -> None:
	task = run_task_on_server(
		server=server_name,
		script="phase5-is-active.sh",
		variables={"VIRTUAL_MACHINE_NAME": vm_name},
		timeout_seconds=15,
	)
	assert task.status == "Success", task.stderr


def _assert_is_inactive(server_name: str, vm_name: str) -> None:
	task = run_task_on_server(
		server=server_name,
		script="phase6-is-inactive.sh",
		variables={"VIRTUAL_MACHINE_NAME": vm_name},
		timeout_seconds=15,
	)
	assert task.status == "Success", task.stderr


def _assert_gone(server_name: str, vm_name: str, tap_device: str) -> None:
	task = run_task_on_server(
		server=server_name,
		script="phase6-assert-gone.sh",
		variables={
			"VIRTUAL_MACHINE_NAME": vm_name,
			"TAP_DEVICE": tap_device,
		},
		timeout_seconds=15,
	)
	assert task.status == "Success", task.stderr
