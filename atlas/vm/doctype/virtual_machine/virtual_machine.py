from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, cint, now_datetime

from atlas.vm.metal_client import MetalClient, MetalClientError, throw_metal_error
from atlas.vm.virtual_machine_manager import VirtualMachineManager

_STATUS_BY_STATE = {
	"created": "Creating",
	"running": "Running",
	"stopped": "Stopped",
	"paused": "Paused",
	"failed": "Failed",
	"unknown": "Unknown",
}
DRAFT_EXPIRY_MINUTES = 15


class VirtualMachine(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		disk_mib: DF.Int
		is_draft: DF.Check
		memory_mib: DF.Int
		server: DF.Link
		tenant_id: DF.Int
		vcpus: DF.Int
		virtual_machine_image: DF.Link
	# end: auto-generated types

	def before_insert(self) -> None:
		if not getattr(self.flags, "created_by_virtual_machine_api", False):
			frappe.throw(_("Create Virtual Machines from the Virtual Machine list."))

	def on_trash(self) -> None:
		"""Delete only after Metal confirms that the VM is absent."""
		is_absence_confirmed = getattr(self.flags, "metal_absence_confirmed", False)
		if self.is_draft and not is_absence_confirmed:
			frappe.throw(_("Wait for Virtual Machine creation reconciliation before deletion."))

		if not is_absence_confirmed:
			try:
				MetalClient(frappe.get_doc("Server", self.server)).get_virtual_machine(self.name)
			except MetalClientError as error:
				if not error.is_not_found:
					throw_metal_error(error)
			else:
				frappe.throw(_("Terminate Virtual Machine {0} before deletion.").format(self.name))
		self.release_ip_address()

	def release_ip_address(self) -> None:
		address_name = frappe.db.get_value("Server IP Address", {"virtual_machine": self.name})
		if address_name:
			frappe.get_doc("Server IP Address", address_name).release()

	def get_metal_info(self) -> dict[str, Any]:
		"""Get this VM once for the current document load."""
		cached = getattr(self, "_metal_info_cache", None)
		if cached is not None:
			return cached
		try:
			info = MetalClient(frappe.get_doc("Server", self.server)).get_virtual_machine(self.name)
		except MetalClientError as error:
			if not error.is_not_found:
				frappe.log_error(
					frappe.get_traceback(),
					f"Could not get Virtual Machine {self.name} from Metal",
				)
			info = {}
		self._metal_info_cache = info
		return info

	@property
	def status(self) -> str:
		if self.is_draft:
			return "Unknown"
		info = self.get_metal_info()
		if not info:
			return "Unknown"
		return _STATUS_BY_STATE.get(info.get("state", ""), "Unknown")

	@property
	def desired_state(self) -> str | None:
		return self.get_metal_info().get("desired_state")

	@property
	def error(self) -> str | None:
		return self.get_metal_info().get("error")

	@property
	def hostname(self) -> str | None:
		return self.get_metal_info().get("hostname")

	@property
	def mac(self) -> str | None:
		return (self.get_metal_info().get("network") or {}).get("mac")

	@property
	def egress(self) -> str | None:
		return (self.get_metal_info().get("network") or {}).get("egress")

	@property
	def wireguard_mesh_ipv6(self) -> str | None:
		return (self.get_metal_info().get("network") or {}).get("wireguard_mesh_ipv6")

	@property
	def public_ipv4(self) -> str | None:
		return (self.get_metal_info().get("network") or {}).get("public_ipv4")

	@frappe.whitelist(methods=["POST"])
	def start(self) -> None:
		self.perform_power_action("start")

	@frappe.whitelist(methods=["POST"])
	def stop(self) -> None:
		self.perform_power_action("stop")

	@frappe.whitelist(methods=["POST"])
	def pause(self) -> None:
		self.perform_power_action("pause")

	@frappe.whitelist(methods=["POST"])
	def resume(self) -> None:
		self.perform_power_action("resume")

	@frappe.whitelist(methods=["POST"])
	def terminate(self) -> None:
		"""Ask Metal to remove this VM."""
		frappe.only_for("System Manager")
		try:
			MetalClient(frappe.get_doc("Server", self.server)).terminate_virtual_machine(self.name)
		except MetalClientError as error:
			if not error.is_not_found:
				throw_metal_error(error)

	@frappe.whitelist(methods=["POST"])
	def create_machine_image(
		self,
		title: str,
		cache_image: bool = False,
		memory_snapshot: bool = False,
		memory_snapshot_virtual_cpu_count: int = 0,
		memory_snapshot_memory_mib: int = 0,
		memory_snapshot_disk_mib: int = 0,
	) -> str:
		"""Queue a Machine image transfer from this VM."""
		frappe.only_for("System Manager")
		if self.is_draft:
			frappe.throw(_("Wait for Virtual Machine creation before creating an image."))
		title = title.strip()
		if not title:
			frappe.throw(_("Image title is required."))

		from atlas.vm.virtual_machine_image_manager import VirtualMachineImageManager

		return VirtualMachineImageManager().create_from_virtual_machine(
			self,
			title,
			cache_image=bool(cint(cache_image)),
			memory_snapshot=bool(cint(memory_snapshot)),
			memory_snapshot_virtual_cpu_count=cint(memory_snapshot_virtual_cpu_count),
			memory_snapshot_memory_mib=cint(memory_snapshot_memory_mib),
			memory_snapshot_disk_mib=cint(memory_snapshot_disk_mib),
		)

	@frappe.whitelist(methods=["POST"])
	def replace_ssh_keys(self, ssh_keys: str | list[str]) -> dict[str, Any]:
		"""Replace all authorized SSH keys for this VM."""
		frappe.only_for("System Manager")
		values = frappe.parse_json(ssh_keys) if isinstance(ssh_keys, str) else ssh_keys
		if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
			frappe.throw(_("SSH keys must be a list of strings."))

		try:
			return MetalClient(frappe.get_doc("Server", self.server)).replace_virtual_machine_ssh_keys(
				self.name, values
			)
		except MetalClientError as error:
			throw_metal_error(error)
			raise AssertionError from error

	@frappe.whitelist(methods=["POST"])
	def resize_disk(self, disk_mib: int) -> None:
		"""Ask Metal to increase this VM disk size."""
		frappe.only_for("System Manager")
		disk_mib = int(disk_mib)
		try:
			MetalClient(frappe.get_doc("Server", self.server)).resize_virtual_machine_disk(
				self.name, disk_mib
			)
		except MetalClientError as error:
			throw_metal_error(error)
		self.db_set("disk_mib", disk_mib)

	def perform_power_action(self, action: str) -> None:
		"""Ask Metal to apply one power action."""
		frappe.only_for("System Manager")
		try:
			MetalClient(frappe.get_doc("Server", self.server)).perform_action(self.name, action)
		except MetalClientError as error:
			throw_metal_error(error)


@frappe.whitelist(methods=["POST"])
def create(request: str | dict[str, Any]) -> dict[str, str | bool]:
	"""Create one Atlas VM request and send its intent to Metal."""
	frappe.only_for("System Manager")
	return VirtualMachineManager().create(request)


def reconcile_stale_drafts() -> None:
	"""Resolve old drafts without deletion when the result is uncertain."""
	cutoff = add_to_date(now_datetime(), minutes=-DRAFT_EXPIRY_MINUTES)
	names = frappe.get_all(
		"Virtual Machine",
		filters={"is_draft": 1, "creation": ["<", cutoff]},
		pluck="name",
	)
	for name in names:
		reconcile_stale_draft(name)


def reconcile_stale_draft(name: str) -> None:
	"""Finalize a present VM or delete a confirmed absent draft."""
	virtual_machine = frappe.get_doc("Virtual Machine", name)
	try:
		MetalClient(frappe.get_doc("Server", virtual_machine.server)).get_virtual_machine(name)
	except MetalClientError as error:
		if not error.is_not_found:
			return
		virtual_machine.flags.metal_absence_confirmed = True
		virtual_machine.delete(ignore_permissions=True)
		return
	virtual_machine.db_set("is_draft", 0)
