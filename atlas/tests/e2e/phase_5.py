"""Phase 5 e2e: provision a Firecracker VM and verify it boots."""

import frappe

from atlas.atlas.ssh import run_task
from atlas.tests.e2e._shared import (
	assert_probe,
	ensure_image_on_server,
	ephemeral_public_key,
	phase,
)


def run(reuse: bool = True, keep: bool = True) -> None:
	with phase("phase-5", reuse=reuse, keep=keep) as server:
		image_doc = ensure_image_on_server(server.name)
		image = image_doc.name

		public_key = ephemeral_public_key()

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

		# Negative: temporarily move the image aside.
		_move_image(server.name, image, "aside")
		try:
			raised = False
			try:
				vm.provision()
			except frappe.ValidationError as exception:
				raised = True
				assert "not present" in str(exception).lower() or "missing" in str(exception).lower()
			assert raised, "provision should have raised when image absent"
			vm.reload()
			# Probe failure already marked Failed; ok.
		finally:
			# Always restore the image, even if the assertion above failed —
			# otherwise the next run starts with the rootfs in .bak and the
			# positive path can't recover.
			_move_image(server.name, image, "back")

		# Recover state for the positive path.
		vm.status = "Pending"
		vm.save(ignore_permissions=True)

		vm.provision()
		vm.reload()
		assert vm.status == "Running", vm.status
		assert vm.last_started

		assert_probe(server.name, "phase5-is-active.sh", VIRTUAL_MACHINE_NAME=vm.name)


def _move_image(server_name: str, image: str, direction: str) -> None:
	assert direction in {"aside", "back"}, direction
	image_doc = frappe.get_doc("Virtual Machine Image", image)
	task = run_task(
		server=server_name,
		script="phase5-move-image.sh",
		variables={
			"IMAGE_NAME": image_doc.image_name,
			"ROOTFS_FILENAME": image_doc.rootfs_filename,
			"DIRECTION": direction,
		},
		timeout_seconds=15,
	)
	assert task.status == "Success"
