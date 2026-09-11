from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _, request_cache
from frappe.model.document import Document
from frappe.utils import add_to_date, cint, now_datetime

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.exceptions import AtlasUserError
from atlas.atlas.core.parsing import strict_bool
from atlas.atlas.doctype.ssh_task.ssh_task import delete_tasks_for_target
from atlas.vm.core import reconciliation
from atlas.vm.core.metal_models import MetalVirtualMachine
from atlas.vm.core.models import VirtualMachineCreateRequest
from atlas.vm.core.vm_service import VirtualMachineService

DRAFT_EXPIRY_MINUTES = 2
# Atlas WG Mesh reserves tenant 0 for the privileged tenant.
PRIVILEGED_TENANT_ID = 0
IMAGE_TYPES = ("machine", "system")


class VirtualMachine(Document):
	"""One requested virtual machine. Runtime values read through to Metal."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		disk_mib: DF.Int
		is_draft: DF.Check
		is_privileged: DF.Check
		is_terminating: DF.Check
		memory_mib: DF.Int
		metadata: DF.Code | None
		server: DF.Link
		sleep_after_idle_seconds: DF.Int
		tenant_id: DF.Int
		vcpus: DF.Int
		virtual_machine_image: DF.Link
	# end: auto-generated types

	@request_cache
	def get_metal_vm_info(self) -> MetalVirtualMachine | None:
		"""Return the Metal record for this VM, cached for one request.

		A record with no Server holds no Metal state. Frappe reads every virtual
		field to build the new document template, so this runs before placement.
		"""
		if not self.server:
			return None
		return VirtualMachineService(self).get_information()

	def before_insert(self) -> None:
		"""Reject a record created outside the Virtual Machine API."""
		if not getattr(self.flags, "created_by_virtual_machine_api", False):
			frappe.throw(_("Create Virtual Machines from the Virtual Machine list."))

	def validate(self) -> None:
		"""A privileged VM must use tenant 0. Tenant 0 alone is not privileged.

		This runs on every save, because the flag is removable. Removing it drops
		the address from the next whitelist and ends cross-tenant traffic.
		"""
		if self.is_privileged and self.tenant_id != PRIVILEGED_TENANT_ID:
			frappe.throw(_("A privileged Virtual Machine must use tenant {0}.").format(PRIVILEGED_TENANT_ID))

	def on_trash(self) -> None:
		"""Delete only after Metal confirms that the VM is absent."""
		VirtualMachineService(self).validate_deletion()
		delete_tasks_for_target(self.doctype, self.name)
		frappe.db.delete("Virtual Machine State", {"name": self.name})

	@property
	def current_state(self) -> str:
		"""Return the state a user sees. A draft is pending, an absent VM is unknown."""
		if self.is_draft:
			return "pending"

		if self.is_terminating:
			return "terminating"

		information = self.get_metal_vm_info()
		return information.observed.state if information else "unknown"

	@property
	def desired_state(self) -> str | None:
		"""Return the state Metal was asked to reach."""
		information = self.get_metal_vm_info()
		return information.desired.state if information else None

	@property
	def error(self) -> str | None:
		"""Return the last reconciliation failure message, when there is one."""
		information = self.get_metal_vm_info()
		return information.observed.error.message if information and information.observed.error else None

	@property
	def hostname(self) -> str | None:
		"""Return the guest hostname."""
		information = self.get_metal_vm_info()
		return information.desired.guest.hostname if information else None

	@property
	def mac(self) -> str | None:
		"""Return the guest MAC address the host assigned."""
		information = self.get_metal_vm_info()
		return information.observed.network.mac if information else None

	@property
	def egress(self) -> str | None:
		"""Return the requested egress mode."""
		information = self.get_metal_vm_info()
		return information.desired.network.egress if information else None

	@property
	def wireguard_mesh_ipv6(self) -> str | None:
		"""Return the Atlas WG Mesh address of the guest."""
		information = self.get_metal_vm_info()
		return information.desired.network.wireguard_mesh_ipv6 if information else None

	@property
	def public_ipv4(self) -> str | None:
		"""Return the attached public IPv4 address, when there is one."""
		information = self.get_metal_vm_info()
		return information.desired.network.public_ipv4 if information else None

	@property
	def ssh_host(self) -> str:
		"""Return the address an SSH Task connects to."""
		if not self.public_ipv4:
			frappe.throw(
				_("Virtual Machine {0} has no public IPv4 address. Attach one and use uplink egress.").format(
					self.name
				)
			)

		return self.public_ipv4

	@property
	def disk_throughput_mibps(self) -> int:
		"""Return the disk throughput limit. Zero applies no limit."""
		information = self.get_metal_vm_info()
		return information.desired.disk.throughput_mibps if information else 0

	@property
	def disk_iops(self) -> int:
		"""Return the disk IOPS limit. Zero applies no limit."""
		information = self.get_metal_vm_info()
		return information.desired.disk.iops if information else 0

	@property
	def private_network_throughput_mibps(self) -> int:
		"""Return the private network limit. Zero applies no limit."""
		information = self.get_metal_vm_info()
		return information.desired.network.private_network_throughput_mibps if information else 0

	@property
	def public_network_throughput_mibps(self) -> int:
		"""Return the public network limit. Zero applies no limit."""
		information = self.get_metal_vm_info()
		return information.desired.network.public_network_throughput_mibps if information else 0

	@property
	def ssh_keys(self) -> str:
		"""Return the authorized keys as one newline-separated block."""
		information = self.get_metal_vm_info()
		return "\n".join(information.desired.guest.ssh_keys) if information else ""

	@property
	def metadata(self) -> str:
		"""Return the guest metadata as indented JSON."""
		information = self.get_metal_vm_info()
		return json.dumps(information.desired.guest.metadata if information else {}, indent=2)

	@frappe.whitelist(methods=["POST"])
	def start(self) -> None:
		"""Request the running state."""
		self.set_power_state("running")

	@frappe.whitelist(methods=["POST"])
	def stop(self) -> None:
		"""Request the stopped state."""
		self.set_power_state("stopped")

	@frappe.whitelist(methods=["POST"])
	def pause(self) -> None:
		"""Request the paused state."""
		self.set_power_state("paused")

	@frappe.whitelist(methods=["POST"])
	def resume(self) -> None:
		"""Request the running state from paused."""
		self.set_power_state("running")

	@frappe.whitelist(methods=["POST"])
	def set_privileged(self, is_privileged: bool | int | str) -> None:
		"""Set or clear the privileged flag. Only tenant 0 may hold it."""
		self.check_permission("write")
		if self.tenant_id != PRIVILEGED_TENANT_ID:
			frappe.throw(_("Only tenant {0} can hold the privileged flag.").format(PRIVILEGED_TENANT_ID))
		if self.is_draft or self.is_terminating:
			frappe.throw(_("Virtual Machine {0} is not ready for this change.").format(self.name))

		self.is_privileged = strict_bool(is_privileged, "is_privileged")
		self.save()

	@frappe.whitelist(methods=["POST"])
	def terminate(self) -> None:
		"""Ask Metal to remove this VM and release its IP address."""
		self.check_permission("write")
		VirtualMachineService(self).terminate()

	@frappe.whitelist(methods=["POST"])
	def create_machine_image(
		self,
		title: str,
		image_type: str = "machine",
		cache_image: bool = False,
		memory_snapshot: bool = False,
	) -> str:
		"""Queue an image transfer from this VM. A System image needs tenant 0."""
		self.check_permission("write")
		if self.is_draft:
			frappe.throw(_("Wait for Virtual Machine creation before creating an image."), exc=AtlasUserError)
		title = title.strip()
		if not title:
			frappe.throw(_("Image title is required."), exc=AtlasUserError)

		if image_type not in IMAGE_TYPES:
			frappe.throw(
				_("Image type must be one of {0}.").format(", ".join(IMAGE_TYPES)), exc=AtlasUserError
			)

		cache_image = bool(cint(cache_image))
		memory_snapshot = bool(cint(memory_snapshot))
		if (
			image_type == "system" or cache_image or memory_snapshot
		) and self.tenant_id != PRIVILEGED_TENANT_ID:
			frappe.throw(
				_("Only tenant {0} can create a System image or set the host image flags.").format(
					PRIVILEGED_TENANT_ID
				),
				exc=AtlasUserError,
			)

		from atlas.vm.core.vm_image_transfer import VirtualMachineImageTransferService

		return VirtualMachineImageTransferService().create_from_virtual_machine(
			self,
			title,
			image_type=image_type,
			cache_image=cache_image,
			memory_snapshot=memory_snapshot,
		)

	@frappe.whitelist(methods=["POST"])
	def replace_ssh_keys(self, ssh_keys: str | list[str]) -> dict[str, Any]:
		"""Replace all authorized SSH keys for this VM."""
		self.check_permission("write")
		values = frappe.parse_json(ssh_keys) if isinstance(ssh_keys, str) else ssh_keys
		if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
			frappe.throw(_("SSH keys must be a list of strings."), exc=AtlasUserError)

		return VirtualMachineService(self).replace_ssh_keys(values)

	@frappe.whitelist(methods=["POST"])
	def replace_metadata(self, metadata: dict[str, str]) -> dict[str, Any]:
		"""Replace all custom metadata for this VM with a plain string-to-string map."""
		self.check_permission("write")
		if not isinstance(metadata, dict) or any(
			not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items()
		):
			frappe.throw(_("Metadata must be a string-to-string map."), exc=AtlasUserError)

		return VirtualMachineService(self).replace_metadata(metadata)

	@frappe.whitelist(methods=["POST"])
	def attach_ip_address(self, server_ip_address: str) -> dict[str, Any]:
		"""Attach one reserved public IPv4 address without a VM restart."""
		self.check_permission("write")
		self.validate_network_change()
		if frappe.db.exists("Metal Server IP Address", {"virtual_machine": self.name}):
			frappe.throw(_("Detach the current public IPv4 address first."), exc=AtlasUserError)

		return VirtualMachineService(self).attach_ip_address(server_ip_address)

	@frappe.whitelist(methods=["POST"])
	def detach_ip_address(self) -> dict[str, Any]:
		"""Remove the public IPv4 address without a VM restart."""
		self.check_permission("write")
		self.validate_network_change()
		if not frappe.db.exists("Metal Server IP Address", {"virtual_machine": self.name}):
			frappe.throw(_("This Virtual Machine has no public IPv4 address."), exc=AtlasUserError)

		return VirtualMachineService(self).detach_ip_address()

	@frappe.whitelist(methods=["POST"])
	def update_egress(self, egress: str) -> dict[str, Any]:
		"""Change internet reachability without a VM restart. Mesh reachability does not change."""
		return self.update_network({"egress": egress})

	@frappe.whitelist(methods=["POST"])
	def update_network_throughput(
		self, private_network_throughput_mibps: int, public_network_throughput_mibps: int
	) -> dict[str, Any]:
		"""Change the throughput limits in MiB/s without a VM restart. A value of 0 removes the limit."""
		return self.update_network(
			{
				"private_network_throughput_mibps": self.parse_limit(
					private_network_throughput_mibps, _("Network throughput")
				),
				"public_network_throughput_mibps": self.parse_limit(
					public_network_throughput_mibps, _("Network throughput")
				),
			}
		)

	@frappe.whitelist(methods=["POST"])
	def update_disk_limits(self, disk_throughput_mibps: int, disk_iops: int) -> dict[str, Any]:
		"""Change the disk limits in MiB/s and IOPS without a VM restart. 0 removes a limit."""
		return self.update_disk(
			{
				"throughput_mibps": self.parse_limit(disk_throughput_mibps, _("Disk throughput")),
				"iops": self.parse_limit(disk_iops, _("Disk IOPS")),
			}
		)

	def parse_limit(self, value: object, label: str) -> int:
		"""Return one rate limit. A malformed value is an error, not 0."""
		try:
			limit = int(str(value).strip())
		except TypeError, ValueError:
			frappe.throw(_("{0} must be a whole number.").format(label), exc=AtlasUserError)
			raise AssertionError from None
		if limit < 0:
			frappe.throw(_("{0} must not be negative.").format(label), exc=AtlasUserError)
		return limit

	def validate_network_change(self) -> None:
		"""Reject a network change while the request is not ready."""
		if self.is_draft:
			frappe.throw(_("Wait for Virtual Machine creation before a network change."), exc=AtlasUserError)
		if self.is_terminating:
			frappe.throw(_("Virtual Machine {0} is terminating.").format(self.name), exc=AtlasUserError)

	@frappe.whitelist(methods=["POST"])
	def resize_disk(self, disk_mib: int) -> dict[str, Any]:
		"""Ask Metal to increase this VM disk size."""
		return self.update_disk({"size_mib": int(disk_mib)})

	@frappe.whitelist(methods=["POST"])
	def resize_compute(self, vcpus: int, memory_mib: int) -> dict[str, Any]:
		"""Ask Metal to change this VM CPU and memory. The VM must be stopped."""
		return self.update_compute({"virtual_cpu_count": int(vcpus), "memory_mib": int(memory_mib)})

	@frappe.whitelist(methods=["POST"])
	def update_idle_shutdown(self, sleep_after_idle_seconds: int) -> dict[str, Any]:
		"""Change the idle shutdown delay. A value of 0 disables it."""
		return self.update_compute(
			{
				"sleep_after_idle_seconds": self.parse_limit(
					sleep_after_idle_seconds, _("Idle shutdown delay")
				),
			}
		)

	def update_compute(self, changes: dict[str, Any]) -> dict[str, Any]:
		"""Apply selected compute changes."""
		self.check_permission("write")
		if self.is_draft:
			frappe.throw(_("Wait for Virtual Machine creation before a compute change."), exc=AtlasUserError)

		return VirtualMachineService(self).update_compute(changes)

	def update_disk(self, changes: dict[str, int]) -> dict[str, Any]:
		"""Apply selected disk size and limit changes."""
		self.check_permission("write")
		if self.is_draft:
			frappe.throw(_("Wait for Virtual Machine creation before a disk change."), exc=AtlasUserError)

		return VirtualMachineService(self).update_disk(changes)

	def update_network(self, changes: dict[str, Any]) -> dict[str, Any]:
		"""Apply selected egress and throughput changes."""
		self.check_permission("write")
		return VirtualMachineService(self).apply_network_changes(changes)

	def set_power_state(self, state: str) -> None:
		"""Ask Metal to store one desired power state."""
		self.check_permission("write")
		VirtualMachineService(self).set_power_state(state)

	@frappe.whitelist(methods=["POST"])
	def reboot(self) -> None:
		"""Request an in-place VM restart."""
		self.check_permission("write")
		VirtualMachineService(self).request_restart()

	@frappe.whitelist(methods=["POST"])
	def get_console_token(self, mode: str = "tty") -> dict[str, str]:
		"""Issue a one-time token to open this VM console in tty or ssh mode."""
		self.check_permission("read")
		if mode not in {"tty", "ssh"}:
			frappe.throw(_("Console mode must be tty or ssh."))
		from atlas.vm.core.console_token import issue_console_token

		connection = VirtualMachineService(self).get_console_connection(mode)
		return {"token": issue_console_token(connection)}


@frappe.whitelist(methods=["POST"])
def create(request: str | dict[str, Any] | VirtualMachineCreateRequest) -> dict[str, str | bool]:
	"""Create one Atlas VM request and send its intent to Metal."""
	if not frappe.has_permission("Virtual Machine", ptype="create"):
		raise frappe.PermissionError

	try:
		request_value = (
			request
			if isinstance(request, VirtualMachineCreateRequest)
			else VirtualMachineCreateRequest.from_value(request)
		)
	except ValueError as error:
		frappe.throw(_(str(error)))
		raise AssertionError from error
	return VirtualMachineService.create(request_value)


@run_as_admin
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
	reconciliation.reconcile_stale_draft(name)


@run_as_admin
def reconcile_terminating_virtual_machines() -> None:
	"""Delete terminated VMs that Metal reports as absent."""
	names = frappe.get_all("Virtual Machine", filters={"is_terminating": 1}, pluck="name")
	for name in names:
		reconcile_terminating_virtual_machine(name)


def reconcile_terminating_virtual_machine(name: str) -> None:
	"""Delete a terminated VM once Metal confirms it is absent."""
	reconciliation.reconcile_terminating(name)
