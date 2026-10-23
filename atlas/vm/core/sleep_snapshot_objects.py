from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

import frappe

if TYPE_CHECKING:
	from atlas.vm.core.metal_models import MetalVirtualMachine
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine


class SleepSnapshotObject(TypedDict):
	"""Keep cleanup ownership independently of the current Metal host."""

	object_key: str
	upload_id: str
	checkpoint_id: str
	artifact: str


class SleepSnapshotObjects:
	"""Retain remote objects until their checkpoint is replaced or the VM is deleted."""

	def __init__(self, machine: VirtualMachine) -> None:
		self.machine = machine

	def lock(self):
		"""Serialize offload, completion, and termination without a database lock."""
		cache = frappe.cache()
		return cache.lock(cache.make_key(f"vm-sleep:{self.machine.name}"), timeout=600, blocking_timeout=10)

	def load(self) -> list[SleepSnapshotObject]:
		"""Read current references and release the row before any remote call."""
		value = frappe.db.get_value(
			"Virtual Machine", self.machine.name, "sleep_snapshot_objects", for_update=True
		)
		frappe.db.commit()  # nosemgrep
		return frappe.parse_json(value) or []

	def save(self, objects: list[SleepSnapshotObject]) -> None:
		"""Commit references before a remote operation can consume them."""
		frappe.db.set_value(
			"Virtual Machine",
			self.machine.name,
			"sleep_snapshot_objects",
			frappe.as_json(objects),
			update_modified=False,
		)
		frappe.db.commit()  # nosemgrep

	def remember(self, information: MetalVirtualMachine) -> None:
		"""Capture keys before a host can delete or move its checkpoint."""
		snapshot = information.observed.sleep_snapshot
		if snapshot is None:
			return
		objects = self.load()
		changed = False
		for name in ("memory", "disk"):
			artifact = getattr(snapshot, name)
			if artifact is None:
				continue
			key = artifact.object_key
			if not key:
				continue
			stored = next((item for item in objects if item["object_key"] == key), None)
			if stored is None:
				stored = {
					"object_key": key,
					"upload_id": artifact.upload_id,
					"artifact": name,
					"checkpoint_id": snapshot.id,
				}
				objects.append(stored)
				changed = True
			elif stored["checkpoint_id"] != snapshot.id:
				stored["checkpoint_id"] = snapshot.id
				changed = True
		if changed:
			self.save(objects)

	def prepare_upload(self, artifact: str, checkpoint_id: str, object_key: str = "") -> SleepSnapshotObject:
		"""Retain a repeatable upload plan before sending it to Metal."""
		objects = self.load()
		stored = next(
			(
				item
				for item in objects
				if item["artifact"] == artifact and item["checkpoint_id"] == checkpoint_id
			),
			None,
		)
		if stored is None:
			key = (
				object_key
				or f"sleep/{frappe.local.site}/{self.machine.name}/{frappe.generate_hash(length=20)}/{artifact}"
			)
			stored = {
				"object_key": key,
				"upload_id": "",
				"checkpoint_id": checkpoint_id,
				"artifact": artifact,
			}
			objects.append(stored)
			self.save(objects)
		if not stored["upload_id"]:
			storage = frappe.get_cached_doc("Atlas Settings").get_object_storage_client()
			stored["upload_id"] = storage.create_multipart_upload(stored["object_key"])
			self.save(objects)
		return stored

	def delete(self, *, keep_checkpoint: str | None = None) -> None:
		"""Abort retired uploads before deleting objects, retaining failures for retry."""
		objects = self.load()
		retired = [
			item for item in objects if keep_checkpoint is None or item["checkpoint_id"] != keep_checkpoint
		]
		if not retired:
			return
		storage = frappe.get_cached_doc("Atlas Settings").get_object_storage_client()
		for item in retired:
			if item["upload_id"]:
				storage.abort_multipart_upload(item["object_key"], item["upload_id"])
			storage.delete_object(item["object_key"])
			objects.remove(item)
			self.save(objects)
