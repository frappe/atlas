from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import frappe
from frappe.utils import now_datetime

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.object_storage import ObjectStorageError
from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.metal_models import MetalVirtualMachine, timestamp_field
from atlas.vm.core.vm_service import VirtualMachineService

if TYPE_CHECKING:
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine


class VirtualMachineSleepService:
	"""Apply the regional offload policy to one sleeping VM."""

	def __init__(self, virtual_machine: VirtualMachine) -> None:
		self.virtual_machine = virtual_machine

	def reconcile(self) -> None:
		"""Refresh recovery URLs and request artifacts whose idle thresholds have passed."""
		machine = self.virtual_machine
		if machine.is_draft or machine.is_terminating or machine.active_migration or not machine.server:
			return
		client = VirtualMachineService(machine).metal_client
		information = client.get_virtual_machine(machine.name)
		if information.observed.state != "stopped":
			return
		if information.observed.sleep_state not in {"sleeping", "deep-sleeping", "comatose"}:
			return
		settings = frappe.get_cached_doc("Atlas Settings")
		artifacts = self.artifacts_to_offload(
			information,
			settings.deep_sleep_after_idle_seconds,
			settings.coma_after_idle_seconds,
			now_datetime(),
		)
		snapshot = information.observed.sleep_snapshot
		if not artifacts and snapshot is None:
			return
		storage = settings.get_object_storage_client()
		prefix = f"sleep/{frappe.local.site}/{machine.server}/{machine.name}"
		if snapshot is not None:
			refresh = {
				name: {"download_url": storage.object_url(f"{prefix}/{name}")}
				for name in ("memory", "disk")
				if getattr(snapshot, name) is not None
			}
			client.refresh_sleep_state(machine.name, {"snapshot_id": snapshot.id, **refresh})
		if artifacts:
			request = {
				name: {
					"upload_url": storage.object_url(f"{prefix}/{name}", method="PUT"),
					"download_url": storage.object_url(f"{prefix}/{name}"),
				}
				for name in artifacts
			}
			client.offload_sleep_state(machine.name, request)

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
		if coma_seconds and idle_seconds >= coma_seconds and information.observed.sleep_state != "comatose":
			artifacts.append("disk")
		return artifacts


@run_as_admin
def enqueue_sleep_state_reconciliation() -> None:
	"""Queue one policy check and URL refresh for each sleepy VM."""
	for name in frappe.get_all(
		"Virtual Machine",
		filters={"is_draft": 0, "is_terminating": 0, "sleep_after_idle_seconds": [">", 0]},
		pluck="name",
	):
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
	"""Run one repeatable sleep policy check and report failures."""
	if not frappe.db.exists("Virtual Machine", virtual_machine_name):
		return
	try:
		VirtualMachineSleepService(frappe.get_doc("Virtual Machine", virtual_machine_name)).reconcile()
	except MetalClientError, ObjectStorageError, ValueError:
		frappe.log_error(
			title=f"Sleep state reconciliation failed for {virtual_machine_name}",
			message=frappe.get_traceback(with_context=False),
		)
