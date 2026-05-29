"""Use case: sync a Virtual Machine Image to a server.

Operator clicks "Sync to Server" (or "Sync to All Servers") on a
`Virtual Machine Image`. The button enqueues a Task running
`sync-image.sh`, which downloads the kernel and rootfs, verifies SHA-256,
and unpacks the squashfs into an ext4.

This module exercises:

- Happy path: sync the default image, probe the on-server layout, re-sync
  and assert the script short-circuits.
- Image-row validation: `kernel_url` / `rootfs_url` must be https.
- `sync_to_all_servers()` returns one Task per Active Server row.
- `execute_task(task_name)` works synchronously (the worker path used by
  `frappe.enqueue` is queue-agnostic — calling it directly records the
  same branches under coverage instrumentation).
"""

import frappe

from atlas.atlas.ssh import run_task
from atlas.tests.e2e._config import MINIMAL_IMAGE
from atlas.tests.e2e._image import ensure_image_row
from atlas.tests.e2e._shared import (
	DEFAULT_IMAGE,
	ensure_default_image_row,
	expect_validation_error,
	phase,
	wait_for_task,
)


def run(reuse: bool = True, keep: bool = True) -> None:
	"""Full path: HOST sync checks + url validation / sync_to_all count that
	the unit suite also covers. See [run_smoke](#run_smoke)."""
	with phase("image-sync", reuse=reuse, keep=keep) as server:
		image = ensure_default_image_row()

		_clear_cached_rootfs(server.name, image)
		_check_sync_to_server(server.name, image)
		_check_resync_short_circuits(server.name, image)
		_check_execute_task_sync(server.name, image)
		_check_image_url_validation()
		_check_sync_to_all_servers()
		_check_minimal_variant(server.name)


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	"""Host-only path for development. Clears the cached rootfs and runs the
	full download + sha256 + unsquash + mkfs pipeline, probes the on-server
	layout, then asserts the second sync short-circuits.

	Skips url-https validation and the `sync_to_all_servers` count (pure
	logic, covered by `virtual_machine_image/test_virtual_machine_image.py`),
	the in-process `execute_task` duplicate, and the minimal-image variant —
	a second 900s download that proves the same pipeline as default."""
	with phase("image-sync (smoke)", reuse=reuse, keep=keep) as server:
		image = ensure_default_image_row()
		_clear_cached_rootfs(server.name, image)
		_check_sync_to_server(server.name, image)
		_check_resync_short_circuits(server.name, image)


def _check_minimal_variant(server_name: str) -> None:
	"""The minimal cloud image syncs and probes exactly like server: same
	sync-image.sh path (zstd kernel extract + normalize), different rootfs.
	Clearing first turns this into a real download+build, not a short-circuit.
	"""
	image = ensure_image_row(MINIMAL_IMAGE)
	_clear_cached_rootfs(server_name, image)
	_check_sync_to_server(server_name, image)


def _clear_cached_rootfs(server_name: str, image) -> None:
	"""Delete the on-server rootfs so sync-image.sh re-runs the full pipeline.

	With a shared droplet, the rootfs accumulates across e2e runs and the
	short-circuit at sync-image.sh:47 hides any change in the download +
	normalize + mkfs path. Clearing here turns every run into a real
	regression test of the sync script.
	"""
	from atlas.atlas.ssh import run_task

	task = run_task(
		server=server_name,
		script="phase4-clear-image.sh",
		variables={
			"IMAGE_NAME": image.image_name,
			"ROOTFS_FILENAME": image.rootfs_filename,
		},
		timeout_seconds=15,
	)
	assert task.status == "Success", task.stderr


def _check_sync_to_server(server_name: str, image) -> None:
	"""Sync the image to the server and probe the on-server layout."""
	task_name = image.sync_to_server(server_name)
	task = wait_for_task(task_name, timeout_seconds=900, poll_seconds=5)
	assert task.status == "Success", f"sync-image failed: {(task.stderr or '')[:500]}"

	probe = run_task(
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
	assert probe.status == "Success", f"probe failed: {probe.stderr[:500]}"


def _check_resync_short_circuits(server_name: str, image) -> None:
	"""Second sync of the same image is a no-op on the host side."""
	task_name = image.sync_to_server(server_name)
	task = wait_for_task(task_name, timeout_seconds=120, poll_seconds=2)
	assert task.status == "Success"
	assert "already" in task.stdout.lower()


def _check_execute_task_sync(server_name: str, image) -> None:
	"""Insert a Pending sync-image Task and call execute_task() in-process.

	`image.sync_to_server` does the same insertion path but enqueues; we
	insert by hand and call execute_task directly so the runner's branches
	are recorded by `coverage run` (the worker process is not instrumented).
	"""
	from atlas.atlas._ssh.runner import execute_task

	variables = {
		"IMAGE_NAME": image.image_name,
		"KERNEL_URL": image.kernel_url,
		"KERNEL_FILENAME": image.kernel_filename,
		"KERNEL_SHA256": image.kernel_sha256,
		"ROOTFS_URL": image.rootfs_url,
		"ROOTFS_FILENAME": image.rootfs_filename,
		"ROOTFS_SHA256": image.rootfs_sha256,
		"DEFAULT_DISK_GB": str(image.default_disk_gigabytes),
		"GUEST_NETWORK_UNIT": "/tmp/atlas/atlas-network.service",
	}
	task = frappe.get_doc({
		"doctype": "Task",
		"server": server_name,
		"script": "sync-image.sh",
		"status": "Pending",
		"triggered_by": "Administrator",
	})
	task.variables_dict = variables
	task.insert(ignore_permissions=True)
	frappe.db.commit()

	execute_task(task.name)

	final = wait_for_task(task.name, timeout_seconds=120, poll_seconds=1)
	assert final.status == "Success", (final.status, (final.stderr or "")[:300])


def _check_image_url_validation() -> None:
	"""Both kernel_url and rootfs_url must be https."""
	with expect_validation_error("must be an https"):
		frappe.get_doc({
			"doctype": "Virtual Machine Image",
			"image_name": "usecase-bad-kernel",
			"kernel_url": "http://example.com/kernel",
			"kernel_filename": "k",
			"kernel_sha256": "0" * 64,
			"rootfs_url": "https://example.com/rootfs",
			"rootfs_filename": "r",
			"rootfs_sha256": "0" * 64,
			"default_disk_gigabytes": 1,
		}).insert(ignore_permissions=True)

	with expect_validation_error("must be an https"):
		frappe.get_doc({
			"doctype": "Virtual Machine Image",
			"image_name": "usecase-bad-rootfs",
			"kernel_url": "https://example.com/kernel",
			"kernel_filename": "k",
			"kernel_sha256": "0" * 64,
			"rootfs_url": "ftp://example.com/rootfs",
			"rootfs_filename": "r",
			"rootfs_sha256": "0" * 64,
			"default_disk_gigabytes": 1,
		}).insert(ignore_permissions=True)


def _check_sync_to_all_servers() -> None:
	"""sync_to_all_servers() returns one Task per Active Server row."""
	image = frappe.get_doc("Virtual Machine Image", DEFAULT_IMAGE["image_name"])
	active_count = frappe.db.count("Server", filters={"status": "Active"})
	tasks = image.sync_to_all_servers()
	assert len(tasks) == active_count, (len(tasks), active_count)
