from datetime import datetime

import frappe
from frappe import request_cache
from frappe.model.document import Document

from atlas.vm.core.metal_models import MetalCPUBurstStatus
from atlas.vm.core.vm_service import VirtualMachineService


class VirtualMachineState(Document):
	"""The last state a Metal host reported for one virtual machine."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		download_urls_expire_at: DF.Datetime | None
		last_request_at: DF.Datetime | None
		server: DF.Link
		sleep_checkpoint_id: DF.Data | None
		sleep_state: DF.Literal["", "sleeping", "deep-sleeping", "coma"]
		status: DF.Data
		synced_at: DF.Datetime
		virtual_machine: DF.Link
	# end: auto-generated types

	@request_cache
	def get_cpu_burst_status(self) -> MetalCPUBurstStatus | None:
		"""Return Metal's CPU burst status of a burstable VM, read once per request.

		Frappe reads virtual fields only to show the form, so a host sync save does not call Metal.
		"""
		if not frappe.db.get_value("Virtual Machine", self.virtual_machine, "is_burstable"):
			return None
		return VirtualMachineService(
			frappe.get_doc("Virtual Machine", self.virtual_machine)
		).get_cpu_burst_status()

	@property
	def baseline_cpu_millicores(self) -> int | None:
		status = self.get_cpu_burst_status()
		return status.baseline_cpu_millicores if status else None

	@property
	def sliding_window_minutes(self) -> int | None:
		status = self.get_cpu_burst_status()
		return status.sliding_window_minutes if status else None

	@property
	def reset_at(self) -> datetime | None:
		status = self.get_cpu_burst_status()
		return status.reset_at if status else None

	@property
	def up_seconds(self) -> int | None:
		status = self.get_cpu_burst_status()
		return status.up_seconds if status else None

	@property
	def average_cpu_millicores(self) -> int | None:
		status = self.get_cpu_burst_status()
		return status.average_cpu_millicores if status else None

	@property
	def cpu_limit_millicores(self) -> int | None:
		status = self.get_cpu_burst_status()
		return status.cpu_limit_millicores if status else None

	@property
	def is_limited(self) -> bool:
		status = self.get_cpu_burst_status()
		return bool(status and status.is_limited)

	@property
	def idle_seconds(self) -> int | None:
		status = self.get_cpu_burst_status()
		return status.idle_seconds if status else None
