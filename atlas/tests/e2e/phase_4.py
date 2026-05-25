"""Phase 4 e2e: sync an image to a real server."""

import time
import traceback

import frappe

from atlas.atlas.ssh import run_task_on_server
from atlas.tests.e2e._shared import (
	DEFAULT_IMAGE,
	cleanup_droplet,
	ensure_bootstrapped_server,
	sweep_old_droplets,
	wait_for_task,
)


def run(reuse: bool = True, keep: bool = True) -> None:
	"""Sync the default image to a bootstrapped server.

	With `reuse=True` (default), uses an existing Active+reachable server;
	with `keep=True`, leaves any freshly provisioned droplet behind for the
	next phase to reuse.
	"""
	start_clock = time.monotonic()
	server, client, created_now = ensure_bootstrapped_server(reuse=reuse, keep=keep)
	sweep_old_droplets(client)
	image = _ensure_image()

	try:
		task_name = image.sync_to_server(server.name)
		task = wait_for_task(task_name, timeout_seconds=900, poll_seconds=5)
		assert task.status == "Success", f"sync-image failed: {(task.stderr or '')[:500]}"

		_assert_image_on_server(server.name, image)

		# Idempotency: re-sync should short-circuit.
		task_name = image.sync_to_server(server.name)
		task = wait_for_task(task_name, timeout_seconds=120, poll_seconds=2)
		assert task.status == "Success"
		assert "already" in task.stdout.lower()
	except Exception:
		elapsed = time.monotonic() - start_clock
		print(f"phase-4: FAIL in {elapsed:.0f}s")
		traceback.print_exc()
		raise
	finally:
		if created_now and not keep and server.provider_resource_id:
			cleanup_droplet(client, int(server.provider_resource_id))

	elapsed = time.monotonic() - start_clock
	print(f"phase-4: OK in {elapsed:.0f}s")


def _ensure_image() -> "frappe.model.document.Document":
	name = DEFAULT_IMAGE["image_name"]
	if frappe.db.exists("Virtual Machine Image", name):
		doc = frappe.get_doc("Virtual Machine Image", name)
		doc.update(DEFAULT_IMAGE)
		doc.is_active = 1
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return doc
	doc = {"doctype": "Virtual Machine Image", **DEFAULT_IMAGE, "is_active": 1}
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def _assert_image_on_server(server_name: str, image) -> None:
	task = run_task_on_server(
		server=server_name,
		script="phase4-probe.sh",
		variables={
			"IMAGE_NAME": image.image_name,
			"KERNEL_FILENAME": image.kernel_filename,
			"ROOTFS_FILENAME": image.rootfs_filename,
			"DEFAULT_DISK_GB": str(image.default_disk_gigabytes),
		},
		timeout_seconds=60,
	)
	assert task.status == "Success", f"probe failed: {task.stderr[:500]}"
