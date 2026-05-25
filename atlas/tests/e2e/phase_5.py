"""Phase 5 e2e: provision a Firecracker VM and verify it boots."""

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

	keypair_dir = _make_ephemeral_keypair()
	public_key = (open(f"{keypair_dir}/id.pub").read()).strip()

	vm = frappe.get_doc({
		"doctype": "Virtual Machine",
		"description": "phase 5 e2e",
		"server": server.name,
		"image": image,
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 4,
		"ssh_public_key": public_key,
	}).insert(ignore_permissions=True)

	try:
		# Negative: temporarily move the image aside.
		_move_image_aside(server.name, image)
		raised = False
		try:
			vm.provision()
		except frappe.ValidationError as exception:
			raised = True
			assert "not present" in str(exception).lower() or "missing" in str(exception).lower()
		assert raised, "provision should have raised when image absent"
		vm.reload()
		# Probe failure already marked Failed; ok.
		_move_image_back(server.name, image)

		# Recover state for the positive path.
		vm.status = "Pending"
		vm.save(ignore_permissions=True)

		vm.provision()
		vm.reload()
		assert vm.status == "Running", vm.status
		assert vm.last_started

		_assert_is_active_on_server(server.name, vm.name)
	except Exception:
		elapsed = time.monotonic() - start_clock
		print(f"phase-5: FAIL in {elapsed:.0f}s")
		traceback.print_exc()
		raise
	finally:
		if created_now and not keep and server.provider_resource_id:
			cleanup_droplet(client, int(server.provider_resource_id))

	elapsed = time.monotonic() - start_clock
	print(f"phase-5: OK in {elapsed:.0f}s")


def _make_ephemeral_keypair() -> str:
	directory = "/tmp/atlas-e2e-keys"
	os.makedirs(directory, exist_ok=True)
	key_path = f"{directory}/id"
	if not os.path.exists(key_path):
		subprocess.run(
			["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path],
			check=True,
		)
	os.chmod(key_path, 0o600)
	return directory


def _move_image_aside(server_name: str, image: str) -> None:
	image_doc = frappe.get_doc("Virtual Machine Image", image)
	task = run_task_on_server(
		server=server_name,
		script="phase5-move-image.sh",
		variables={
			"IMAGE_NAME": image_doc.image_name,
			"ROOTFS_FILENAME": image_doc.rootfs_filename,
			"DIRECTION": "aside",
		},
		timeout_seconds=15,
	)
	assert task.status == "Success"


def _move_image_back(server_name: str, image: str) -> None:
	image_doc = frappe.get_doc("Virtual Machine Image", image)
	task = run_task_on_server(
		server=server_name,
		script="phase5-move-image.sh",
		variables={
			"IMAGE_NAME": image_doc.image_name,
			"ROOTFS_FILENAME": image_doc.rootfs_filename,
			"DIRECTION": "back",
		},
		timeout_seconds=15,
	)
	assert task.status == "Success"


def _assert_is_active_on_server(server_name: str, vm_name: str) -> None:
	task = run_task_on_server(
		server=server_name,
		script="phase5-is-active.sh",
		variables={"VIRTUAL_MACHINE_NAME": vm_name},
		timeout_seconds=15,
	)
	assert task.status == "Success", task.stderr
