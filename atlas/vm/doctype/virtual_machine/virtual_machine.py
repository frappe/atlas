from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _, request_cache
from frappe.model.document import Document
from frappe.model.naming import make_autoname
from frappe.utils import add_to_date, cint, now_datetime

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.exceptions import AtlasConflictError, AtlasUserError
from atlas.atlas.core.parsing import strict_bool
from atlas.atlas.core.tags import validate_tags
from atlas.atlas.doctype.ssh_task.ssh_task import delete_tasks_for_target
from atlas.vm.core import reconciliation
from atlas.vm.core.metal_models import MetalVirtualMachine
from atlas.vm.core.models import (
	IPV6_INTERNET_DESTINATION,
	ROUTE_VIA_HOST,
	Route,
	VirtualMachineCreateRequest,
)
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

		from atlas.atlas.doctype.atlas_tag.atlas_tag import AtlasTag

		active_migration: DF.Link | None
		architecture: DF.Literal["amd64", "arm64"]
		cpu_millicores: DF.Int
		disk_mib: DF.Int
		firewall_summary: DF.Code | None
		is_draft: DF.Check
		is_network_gateway: DF.Check
		is_privileged: DF.Check
		is_terminating: DF.Check
		is_termination_protected: DF.Check
		memory_mib: DF.Int
		metadata: DF.Code | None
		routes: DF.Code | None
		server: DF.Link
		sleep_after_idle_seconds: DF.Int
		tags: DF.Table[AtlasTag]
		tenant_id: DF.Int
		virtual_machine_image: DF.Data
	# end: auto-generated types

	def autoname(self) -> None:
		"""Assign a permanent virtual machine ID."""
		self.name = make_autoname("vm-.#######", doc=self)

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
		validate_tags(self)

		if self.is_privileged and self.tenant_id != PRIVILEGED_TENANT_ID:
			frappe.throw(_("A privileged Virtual Machine must use tenant {0}.").format(PRIVILEGED_TENANT_ID))

		if self.is_network_gateway:
			self.validate_network_gateway()

	def validate_network_gateway(self) -> None:
		"""A gateway carries traffic for other tenants, so it must be privileged."""
		if not self.is_privileged:
			frappe.throw(_("A network gateway needs the privileged flag."), exc=AtlasUserError)

	def on_trash(self) -> None:
		"""Delete only after Metal confirms that the VM is absent."""
		self.ensure_not_termination_protected()
		VirtualMachineService(self).validate_deletion()
		delete_tasks_for_target(self.doctype, self.name)
		if frappe.db.exists("Virtual Machine State", self.name):
			frappe.delete_doc(
				"Virtual Machine State", self.name, ignore_permissions=True, delete_permanently=True
			)
		# Migration records link to the VM and must be deleted with it.
		for migration in frappe.get_all(
			"Virtual Machine Migration", filters={"virtual_machine": self.name}, pluck="name"
		):
			frappe.delete_doc(
				"Virtual Machine Migration", migration, ignore_permissions=True, delete_permanently=True
			)

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
	def wireguard_mesh_ipv6(self) -> str | None:
		"""Return the Atlas WG Mesh address of the guest."""
		information = self.get_metal_vm_info()
		return information.desired.network.wireguard_mesh_ipv6 if information else None

	@property
	def public_ipv4(self) -> str | None:
		"""Return the public IPv4 address assigned in Atlas."""
		prefix = frappe.db.get_value(
			"Public IP Allocation", {"virtual_machine": self.name, "version": "4"}, "prefix"
		)
		return prefix.partition("/")[0] if prefix else None

	@property
	def public_ipv6(self) -> str:
		"""Return the IPv6 block assigned in Atlas."""
		return VirtualMachineService(self).get_public_ipv6()

	@property
	def routed_ipv6(self) -> str:
		"""Return the routed public address of this VM, or an empty string."""
		row = frappe.db.get_value(
			"Public IP Allocation",
			{"virtual_machine": self.name, "version": "6"},
			["prefix", "pool"],
			as_dict=True,
		)
		if not row or not frappe.db.get_value("Public IP Pool", row.pool, "gateway"):
			return ""
		return row.prefix.partition("/")[0]

	@property
	def routes(self) -> str:
		"""Return the routes that Metal holds."""
		information = self.get_metal_vm_info()
		routes = information.desired.network.routes if information else ()
		return json.dumps([route.as_dict() for route in routes], indent=2)

	@property
	def ssh_host(self) -> str:
		"""Return the address an SSH Task connects to."""
		if not self.public_ipv4:
			frappe.throw(
				_("Virtual Machine {0} has no public IPv4 address. Attach one first.").format(self.name)
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
	def firewall_summary(self) -> str:
		"""Return the desired firewall that Metal holds."""
		information = self.get_metal_vm_info()
		if not information:
			return json.dumps({"enabled": False, "inbound": [], "outbound": []}, indent=2)
		return json.dumps(information.desired.network.firewall.as_dict(), indent=2)

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
		self.ensure_not_migrating()

		self.is_privileged = strict_bool(is_privileged, "is_privileged")
		self.save()

	@frappe.whitelist(methods=["POST"])
	def set_routes(self, routes: list[dict[str, str]] | str | None = None) -> None:
		"""Replace the host or gateway address that carries each destination range."""
		self.check_permission("write")
		self.ensure_not_migrating()
		self.validate_network_change()
		VirtualMachineService(self).set_routes(frappe.parse_json(routes) or [])

	@frappe.whitelist(methods=["GET"])
	def read_routes(self) -> list[dict[str, str]]:
		"""Return the routes with optional gateway VM names for the editor."""
		self.check_permission("read")
		return VirtualMachineService(self).get_route_editor_rows()

	@frappe.whitelist(methods=["POST"])
	def set_network_gateway(self, is_network_gateway: bool | int | str) -> None:
		"""Let this VM carry traffic for other VMs, or stop it."""
		self.check_permission("write")
		self.ensure_not_migrating()
		self.validate_network_change()
		service = VirtualMachineService(self)
		service.lock_network()
		self.is_network_gateway = strict_bool(is_network_gateway, "is_network_gateway")
		changes: dict[str, Any] = {"is_network_gateway": bool(self.is_network_gateway)}
		if self.is_network_gateway:
			self.validate_network_gateway()
			if service.has_gateway_routes():
				frappe.throw(
					_("Remove the gateway routes of this VM before it becomes a gateway."), exc=AtlasUserError
				)
			changes["routes"] = service.get_routes_with(Route(IPV6_INTERNET_DESTINATION, ROUTE_VIA_HOST))
		service.update_network(changes)
		self.save()

	@frappe.whitelist(methods=["POST"])
	def terminate(self) -> None:
		"""Ask Metal to remove this VM and release its IP address."""
		self.check_permission("write")
		self.ensure_not_termination_protected()
		self.ensure_not_migrating()
		VirtualMachineService(self).terminate()

	@frappe.whitelist(methods=["POST"])
	def set_termination_protection(self, is_protected: bool | int | str) -> None:
		"""Set or clear termination protection."""
		self.check_permission("write")
		if self.is_terminating:
			frappe.throw(_("Virtual Machine {0} is terminating.").format(self.name), exc=AtlasUserError)

		self.is_termination_protected = strict_bool(is_protected, "is_protected")
		self.save()

	def ensure_not_termination_protected(self) -> None:
		"""Reject removal while termination protection holds this VM."""
		if self.is_termination_protected:
			frappe.throw(
				_("Virtual Machine {0} is termination protected.").format(self.name), exc=AtlasConflictError
			)

	@frappe.whitelist(methods=["POST"])
	def migrate(self, destination_metal_server: str | None = None) -> str:
		"""Schedule this VM for migration, with an optional destination Metal Server."""
		self.check_permission("write")
		from atlas.vm.core.vm_migration import MigrationService

		return MigrationService.create(self, destination_metal_server=destination_metal_server or None)

	def ensure_not_migrating(self) -> None:
		"""Reject a mutable action while a migration owns this VM."""
		if self.active_migration:
			frappe.throw(_("Virtual Machine {0} is migrating.").format(self.name), exc=AtlasUserError)

	@frappe.whitelist(methods=["POST"])
	def create_machine_image(
		self,
		title: str,
		image_type: str = "machine",
		cache_image: bool = False,
		memory_snapshot: bool = False,
		memory_snapshot_configuration: dict[str, int] | None = None,
		is_termination_protected: bool = False,
		tags: dict[str, str] | None = None,
	) -> str:
		"""Queue an image transfer from this VM. A System image needs tenant 0. An absent
		memory snapshot value keeps this VM shape."""
		self.check_permission("write")
		self.ensure_not_migrating()
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

		if memory_snapshot_configuration and not memory_snapshot:
			frappe.throw(_("Memory snapshot configuration needs a memory snapshot."), exc=AtlasUserError)

		from atlas.vm.core.vm_image_transfer import VirtualMachineImageTransferService

		return VirtualMachineImageTransferService().create_from_virtual_machine(
			self,
			title,
			image_type=image_type,
			cache_image=cache_image,
			memory_snapshot=memory_snapshot,
			memory_snapshot_configuration=memory_snapshot_configuration,
			is_termination_protected=strict_bool(is_termination_protected, "is_termination_protected"),
			tags=tags,
		)

	@frappe.whitelist(methods=["POST"])
	def replace_ssh_keys(self, ssh_keys: str | list[str]) -> dict[str, Any]:
		"""Replace all authorized SSH keys for this VM."""
		self.check_permission("write")
		self.ensure_not_migrating()
		values = frappe.parse_json(ssh_keys) if isinstance(ssh_keys, str) else ssh_keys
		if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
			frappe.throw(_("SSH keys must be a list of strings."), exc=AtlasUserError)

		return VirtualMachineService(self).replace_ssh_keys(values)

	@frappe.whitelist(methods=["POST"])
	def replace_metadata(self, metadata: dict[str, str]) -> dict[str, Any]:
		"""Replace all custom metadata for this VM with a plain string-to-string map."""
		self.check_permission("write")
		self.ensure_not_migrating()
		try:
			metadata = VirtualMachineCreateRequest.metadata_map({"metadata": metadata})
		except ValueError as error:
			frappe.throw(_(str(error)), exc=AtlasUserError)

		return VirtualMachineService(self).replace_metadata(metadata)

	@frappe.whitelist(methods=["POST"])
	def attach_public_ip(self, version: int, allocation: str) -> None:
		"""Store one public IP attachment intent."""
		self.check_permission("write")
		from atlas.metal_server.core.public_ip_service import PublicIPService

		PublicIPService().attach(self, int(version), allocation)

	@frappe.whitelist(methods=["POST"])
	def detach_public_ip(self, version: int) -> None:
		"""Store one public IP detach intent."""
		self.check_permission("write")
		from atlas.metal_server.core.public_ip_service import PublicIPService

		PublicIPService().detach(self, int(version))

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
	def update_firewall(self, firewall: dict[str, Any]) -> dict[str, Any]:
		"""Change the firewall without a VM restart."""
		return self.update_network({"firewall": firewall})

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
		size_mib = self.parse_limit(disk_mib, _("Disk size"))
		if size_mib == 0:
			frappe.throw(_("Disk size must be positive."), exc=AtlasUserError)
		return self.update_disk({"size_mib": size_mib})

	@frappe.whitelist(methods=["POST"])
	def resize(
		self,
		cpu_millicores: int | None = None,
		memory_mib: int | None = None,
		disk_mib: int | None = None,
		sleep_after_idle_seconds: int | None = None,
	) -> str | None:
		"""Change CPU, memory, disk size, or idle shutdown delay. Return the migration name when the
		current host cannot hold the new shape."""
		self.check_permission("write")
		self.ensure_not_migrating()
		if self.is_draft:
			frappe.throw(_("Wait for Virtual Machine creation before a resize."), exc=AtlasUserError)
		if self.is_terminating:
			frappe.throw(_("Virtual Machine {0} is terminating.").format(self.name), exc=AtlasUserError)

		from atlas.vm.core.vm_resize import VirtualMachineResize

		values = {
			"cpu_millicores": cpu_millicores,
			"memory_mib": memory_mib,
			"disk_mib": disk_mib,
			"sleep_after_idle_seconds": sleep_after_idle_seconds,
		}
		if any(isinstance(value, (bool, float)) for value in values.values() if value is not None):
			frappe.throw(_("Resize values must be integers."), exc=AtlasUserError)
		try:
			changes = {field: int(value) for field, value in values.items() if value is not None}
		except TypeError, ValueError:
			frappe.throw(_("Resize values must be integers."), exc=AtlasUserError)
		return VirtualMachineResize(self).apply(changes)

	def update_disk(self, changes: dict[str, int]) -> dict[str, Any]:
		"""Apply selected disk size and limit changes."""
		self.check_permission("write")
		self.ensure_not_migrating()
		if self.is_draft:
			frappe.throw(_("Wait for Virtual Machine creation before a disk change."), exc=AtlasUserError)

		return VirtualMachineService(self).update_disk(changes)

	def update_network(self, changes: dict[str, Any]) -> dict[str, Any]:
		"""Apply selected network changes."""
		self.check_permission("write")
		self.ensure_not_migrating()
		return VirtualMachineService(self).apply_network_changes(changes)

	def set_power_state(self, state: str) -> None:
		"""Ask Metal to store one desired power state."""
		self.check_permission("write")
		self.ensure_not_migrating()
		VirtualMachineService(self).set_power_state(state)

	@frappe.whitelist(methods=["POST"])
	def reboot(self) -> None:
		"""Request an in-place VM restart."""
		self.check_permission("write")
		self.ensure_not_migrating()
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


def on_doctype_update() -> None:
	"""Index each aggregate that placement reads, so none of them scans the fleet."""
	frappe.db.add_index("Virtual Machine", ["server", "creation", "is_draft"], "placement_vm_usage")
	frappe.db.add_index("Virtual Machine", ["creation", "server"], "placement_rate")
	frappe.db.add_index("Virtual Machine", ["tenant_id", "server"], "placement_tenant_hosts")
	frappe.db.add_index(
		"Virtual Machine",
		["server", "sleep_after_idle_seconds", "memory_mib"],
		"placement_sleepy_memory",
	)
