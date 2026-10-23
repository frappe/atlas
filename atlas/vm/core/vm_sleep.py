from __future__ import annotations

from datetime import datetime
from functools import cached_property
from typing import TYPE_CHECKING

import frappe
from frappe.query_builder.functions import Coalesce
from frappe.utils import add_to_date, now_datetime

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.object_storage import ObjectStorageClient, ObjectStorageError
from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.metal_models import MetalSleepSnapshot, MetalVirtualMachine, timestamp_field
from atlas.vm.core.models import MAXIMUM_OFFLOAD_DISK_MIB, MAXIMUM_OFFLOAD_MEMORY_MIB
from atlas.vm.core.reconciliation import log_failure
from atlas.vm.core.vm_service import VirtualMachineService
from atlas.vm.core.vm_state import SLEEP_STATES

if TYPE_CHECKING:
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine

# An object store accepts at most 5 GiB in one upload, so Metal stores an artifact in parts.
SLEEP_PART_SIZE_MIB = 512
ARTIFACTS = ("memory", "disk")


class VirtualMachineSleepService:
	"""Apply the VM's offload policy to its sleeping checkpoint and keep its objects readable."""

	def __init__(self, virtual_machine: VirtualMachine) -> None:
		self.virtual_machine = virtual_machine

	@cached_property
	def storage(self) -> ObjectStorageClient:
		return frappe.get_cached_doc("Atlas Settings").get_object_storage_client()

	def reconcile(self) -> None:
		"""Upload due artifacts, renew download URLs, and delete objects that no checkpoint uses."""
		machine = self.virtual_machine
		if machine.is_draft or machine.is_terminating or machine.active_migration or not machine.server:
			return
		client = VirtualMachineService(machine).metal_client
		information = client.get_virtual_machine(machine.name)

		observed = information.observed
		snapshot = None
		if observed.sleep_state in SLEEP_STATES and observed.state == "stopped":
			artifacts = []
			if is_offload_allowed(machine.memory_mib, machine.disk_mib):
				artifacts = self.artifacts_to_offload(
					information,
					machine.deep_sleep_after_idle_seconds,
					machine.coma_after_idle_seconds,
					now_datetime(),
				)
			if artifacts:
				information = self.offload(information, client, artifacts)
			self.refresh_urls(information, client)
			snapshot = information.observed.sleep_snapshot
			if snapshot is None and observed.sleep_state != "sleeping":
				return
		elif observed.sleep_state:
			# A VM in a sleep transition can still need every object, so nothing is deleted.
			return

		self.delete_unused_objects(snapshot)
		self.set_download_urls_expire_at(self.new_urls_expire_at if snapshot else None)

	def offload(
		self, information: MetalVirtualMachine, client: MetalClient, artifacts: list[str]
	) -> MetalVirtualMachine:
		"""Sign the parts of each artifact and ask Metal to upload them."""
		snapshot = information.observed.sleep_snapshot
		checkpoint_id = snapshot.id if snapshot else frappe.generate_hash(length=16)
		directory = f"sleep/{frappe.local.site}/{self.virtual_machine.name}/{checkpoint_id}"
		sizes_mib = {"memory": self.virtual_machine.memory_mib, "disk": self.virtual_machine.disk_mib}
		request = {}
		for name in artifacts:
			artifact = getattr(snapshot, name) if snapshot else None
			key = artifact.object_key if artifact and artifact.object_key else f"{directory}/{name}"
			# Compression can grow incompressible data a little, and memory carries tar headers.
			count = sizes_mib[name] // SLEEP_PART_SIZE_MIB + 2
			request[name] = {
				"object_key": key,
				"part_size_bytes": SLEEP_PART_SIZE_MIB << 20,
				"parts": [
					{
						"upload_url": self.storage.object_url(f"{key}.{number}", method="PUT"),
						"download_url": self.storage.object_url(f"{key}.{number}"),
					}
					for number in range(1, count + 1)
				],
			}
		# Commit the expiry before the upload starts, so a failure after it still leads to cleanup.
		self.set_download_urls_expire_at(self.new_urls_expire_at)
		frappe.db.commit()  # nosemgrep
		return client.offload_sleep_state(
			self.virtual_machine.name, {"snapshot_id": checkpoint_id, **request}
		)

	def refresh_urls(self, information: MetalVirtualMachine, client: MetalClient) -> None:
		"""Sign new download URLs for each uploaded artifact at its original object key."""
		snapshot = information.observed.sleep_snapshot
		if snapshot is None:
			return
		machine = self.virtual_machine
		refresh = {}
		for name in ARTIFACTS:
			artifact = getattr(snapshot, name)
			if artifact is None or not artifact.uploaded:
				continue
			# An artifact without a key was stored under its first host.
			key = artifact.object_key or f"sleep/{frappe.local.site}/{machine.server}/{machine.name}/{name}"
			if artifact.part_count:
				parts = [
					{"download_url": self.storage.object_url(f"{key}.{number}")}
					for number in range(1, artifact.part_count + 1)
				]
				refresh[name] = {"object_key": key, "parts": parts}
			else:
				refresh[name] = {"object_key": key, "download_url": self.storage.object_url(key)}
		if refresh:
			client.refresh_sleep_state(machine.name, {"snapshot_id": snapshot.id, **refresh})

	def delete_unused_objects(self, snapshot: MetalSleepSnapshot | None) -> None:
		"""Delete each checkpoint directory of the VM except the current checkpoint."""
		machine = self.virtual_machine
		root = f"sleep/{frappe.local.site}/{machine.name}/"
		self.storage.delete_prefix(root, keep={f"{root}{snapshot.id}"} if snapshot else set())
		if snapshot is None:
			self.storage.delete_prefix(f"sleep/{frappe.local.site}/{machine.server}/{machine.name}/")

	@property
	def new_urls_expire_at(self) -> datetime:
		return add_to_date(now_datetime(), seconds=self.storage.signed_url_expiry)

	def set_download_urls_expire_at(self, expire_at: datetime | None) -> None:
		"""Store when the download URLs that Metal holds expire. Empty means no objects."""
		frappe.db.set_value(
			"Virtual Machine State",
			self.virtual_machine.name,
			"download_urls_expire_at",
			expire_at,
			update_modified=False,
		)

	@staticmethod
	def artifacts_to_offload(
		information: MetalVirtualMachine, deep_sleep_seconds: int, coma_seconds: int, now: datetime
	) -> list[str]:
		"""Select artifacts from Metal's last request time, not the time sleep began."""
		if (
			information.desired.state != "running"
			or information.desired.compute.sleep_after_idle_seconds <= 0
		):
			return []
		last_request = timestamp_field(
			{"last_request_at": information.observed.last_request_at}, "last_request_at"
		)
		if last_request is None or not deep_sleep_seconds:
			return []
		idle_seconds = (now - last_request).total_seconds()
		if idle_seconds < deep_sleep_seconds:
			return []
		artifacts = []
		if information.observed.sleep_state == "sleeping":
			artifacts.append("memory")
		if coma_seconds and idle_seconds >= coma_seconds and information.observed.sleep_state != "coma":
			artifacts.append("disk")
		return artifacts

	@staticmethod
	def is_due(row: frappe._dict, now: datetime, renew_before: datetime) -> bool:
		"""Return whether stored host state shows that a VM needs work.

		The work is an upload whose idle threshold has passed, download URLs that
		expire soon, or objects that an awake VM no longer needs.
		"""
		if not row.sleep_state:
			return bool(row.download_urls_expire_at)
		if row.sleep_state not in SLEEP_STATES:
			return False
		if row.sleep_state != "sleeping" and (
			row.download_urls_expire_at is None or row.download_urls_expire_at <= renew_before
		):
			return True
		if row.last_request_at is None or not is_offload_allowed(row.memory_mib, row.disk_mib):
			return False
		idle_seconds = (now - row.last_request_at).total_seconds()
		return bool(
			(row.sleep_state == "sleeping" and row.deep_sleep and idle_seconds >= row.deep_sleep)
			or (row.coma and row.sleep_state != "coma" and idle_seconds >= row.coma)
		)


def is_offload_allowed(memory_mib: int, disk_mib: int) -> bool:
	return memory_mib <= MAXIMUM_OFFLOAD_MEMORY_MIB and disk_mib <= MAXIMUM_OFFLOAD_DISK_MIB


def get_due_virtual_machines(now: datetime) -> list[str]:
	"""Return the sleepy VMs whose stored host state shows due work."""
	machine = frappe.qb.DocType("Virtual Machine")
	state = frappe.qb.DocType("Virtual Machine State")
	rows = (
		frappe.qb.from_(machine)
		.join(state)
		.on(state.name == machine.name)
		.select(
			machine.name,
			machine.memory_mib,
			machine.disk_mib,
			machine.deep_sleep_after_idle_seconds.as_("deep_sleep"),
			machine.coma_after_idle_seconds.as_("coma"),
			state.sleep_state,
			state.last_request_at,
			state.download_urls_expire_at,
		)
		.where(
			(machine.is_draft == 0)
			& (machine.is_terminating == 0)
			& (Coalesce(machine.active_migration, "") == "")
			& (state.server == machine.server)
			& (state.sleep_state.isin(SLEEP_STATES) | state.download_urls_expire_at.isnotnull())
		)
		.run(as_dict=True)
	)
	expiry_seconds = frappe.db.get_single_value("Atlas Settings", "object_storage_signed_url_expiry") or 86400
	# Renew halfway to expiry, so a short Atlas outage cannot leave a VM unable to wake.
	renew_before = add_to_date(now, seconds=expiry_seconds // 2)
	return [row.name for row in rows if VirtualMachineSleepService.is_due(row, now, renew_before)]


@run_as_admin
def enqueue_sleep_state_reconciliation() -> None:
	"""Queue a check for each VM that needs an upload, new download URLs, or object cleanup."""
	for name in get_due_virtual_machines(now_datetime()):
		frappe.enqueue(
			"atlas.vm.core.vm_sleep.reconcile_sleep_state",
			queue="default",
			timeout=180,
			virtual_machine_name=name,
			job_id=f"atlas||vm-sleep||{name}",
			deduplicate=True,
		)


@run_as_admin
def reconcile_sleep_state(virtual_machine_name: str) -> None:
	"""Run one repeatable sleep policy check, and log a failure once an hour."""
	if not frappe.db.exists("Virtual Machine", virtual_machine_name):
		return
	try:
		VirtualMachineSleepService(frappe.get_doc("Virtual Machine", virtual_machine_name)).reconcile()
	except MetalClientError, ObjectStorageError, ValueError:
		log_failure(virtual_machine_name, "sleep reconciliation")


@run_as_admin
def delete_sleep_objects(virtual_machine_name: str, server_name: str) -> None:
	"""Delete every sleep object of a deleted VM."""
	storage = frappe.get_cached_doc("Atlas Settings").get_object_storage_client()
	storage.delete_prefix(f"sleep/{frappe.local.site}/{virtual_machine_name}/")
	storage.delete_prefix(f"sleep/{frappe.local.site}/{server_name}/{virtual_machine_name}/")


@run_as_admin
def delete_orphaned_sleep_objects() -> None:
	"""Delete the sleep objects of deleted VMs, which a failed deletion job leaves behind."""
	settings = frappe.get_cached_doc("Atlas Settings")
	if not settings.is_object_storage_configured:
		return
	storage = settings.get_object_storage_client()
	root = f"sleep/{frappe.local.site}/"
	for name in storage.list_directories(root):
		if frappe.db.exists("Virtual Machine", name):
			continue
		# An older layout keeps a directory per host with a directory per VM inside it.
		children = storage.list_directories(f"{root}{name}/")
		if children and frappe.db.exists("Virtual Machine", {"name": ["in", children]}):
			continue
		storage.delete_prefix(f"{root}{name}/")
