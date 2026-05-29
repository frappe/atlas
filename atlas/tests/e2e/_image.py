"""Image-row helpers for the e2e harness."""

import frappe

from atlas.tests.e2e._config import DEFAULT_IMAGE
from atlas.tests.e2e._tasks import wait_for_task


def ensure_image_row(image_spec: dict) -> "frappe.model.document.Document":
	"""Ensure a `Virtual Machine Image` row matches `image_spec` exactly.

	Image rows are immutable after insert (URLs, checksums, title, …), so a
	stale row from an earlier run with different bytes cannot be `save()`-d
	into the new spec — it would throw "<field> is immutable". The harness
	owns these rows and treats them as disposable: if an existing row differs
	on any spec field, delete and re-insert. Returns the Document. Does not
	touch any server.
	"""
	name = image_spec["image_name"]
	if frappe.db.exists("Virtual Machine Image", name):
		current = frappe.get_doc("Virtual Machine Image", name)
		if current.is_active and all(current.get(k) == v for k, v in image_spec.items()):
			return current
		# Differs (e.g. constants bumped) — drop the immutable row and re-insert.
		_delete_image_and_vms(name)
	image = frappe.get_doc({
		"doctype": "Virtual Machine Image", **image_spec, "is_active": 1,
	}).insert(ignore_permissions=True)
	frappe.db.commit()
	return image


def _delete_image_and_vms(image_name: str) -> None:
	"""Force-delete an image row and any VM rows that Link to it (the Link
	would otherwise block deletion). Test-only — operators archive, never
	delete."""
	for vm in frappe.get_all(
		"Virtual Machine", filters={"image": image_name}, pluck="name"
	):
		frappe.delete_doc("Virtual Machine", vm, force=True, ignore_permissions=True)
	frappe.delete_doc(
		"Virtual Machine Image", image_name, force=True, ignore_permissions=True
	)
	frappe.db.commit()


def ensure_default_image_row() -> "frappe.model.document.Document":
	"""Ensure the default (server) `Virtual Machine Image` row matches
	`DEFAULT_IMAGE`. Thin wrapper over `ensure_image_row`."""
	return ensure_image_row(DEFAULT_IMAGE)


def ensure_image_on_server(server_name: str) -> "frappe.model.document.Document":
	"""Sync DEFAULT_IMAGE to `server_name`. `sync-image.sh` is idempotent and
	short-circuits when the rootfs is already present on disk, so re-calling
	is cheap (one SSH roundtrip).
	"""
	image = ensure_default_image_row()
	task_name = image.sync_to_server(server_name)
	task = wait_for_task(task_name, timeout_seconds=900, poll_seconds=5)
	if task.status != "Success":
		raise AssertionError(f"sync-image failed: {(task.stderr or '')[:500]}")
	return image
