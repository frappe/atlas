from __future__ import annotations

from typing import TYPE_CHECKING, cast

import frappe
from frappe import _

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.models import (
	MAXIMUM_CPU_MILLICORES,
	MAXIMUM_SLEEP_AFTER_IDLE_SECONDS,
	MINIMUM_CPU_MILLICORES,
	VirtualMachineShape,
)
from atlas.vm.core.placement import CurrentPlacement, PlacementRequirements, PlacementStrategy
from atlas.vm.core.placement.transaction import use_read_committed
from atlas.vm.core.vm_migration import MigrationService
from atlas.vm.core.vm_service import VirtualMachineService

if TYPE_CHECKING:
	from atlas.vm.core.metal_models import MetalVirtualMachine
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine

RESIZE_FIELDS = ("cpu_millicores", "memory_mib", "disk_mib", "sleep_after_idle_seconds")


class VirtualMachineResize:
	"""Resize one VM on its host, or move it to a host that holds the new shape."""

	def __init__(self, virtual_machine: VirtualMachine) -> None:
		self.virtual_machine = virtual_machine
		self.service = VirtualMachineService(virtual_machine)

	@use_read_committed
	def apply(self, resize_values: dict[str, int]) -> str | None:
		"""Apply the changed values. Return the migration name when the VM moves."""
		self._validate_values(resize_values)
		information = self.service.require_information()
		current_shape = self._get_current_shape(information)
		target_shape = self._get_target_shape(current_shape, resize_values)
		if target_shape.has_same_resources(current_shape):
			if target_shape != current_shape:
				self._set_idle_shutdown(current_shape, target_shape.sleep_after_idle_seconds)
			self._sync_shape(current_shape)
			return None
		if information.observed.state != "stopped":
			frappe.throw(_("Stop the Virtual Machine before you resize it."), exc=AtlasUserError)

		destination = PlacementStrategy.find_server(
			self._get_requirements(target_shape),
			current_placement=CurrentPlacement(
				cast(str, self.virtual_machine.server), current_shape.memory_mib, current_shape.disk_mib
			),
		)
		if destination == self.virtual_machine.server:
			try:
				self._resize_in_place(target_shape)
				return None
			except MetalClientError as error:
				# The capacity sample was older than the host state.
				if not error.is_insufficient_capacity:
					self.service.raise_metal_error(error)

		# The idle delay needs no capacity, so it changes on the source and the move keeps it.
		if target_shape.sleep_after_idle_seconds != current_shape.sleep_after_idle_seconds:
			self._set_idle_shutdown(current_shape, target_shape.sleep_after_idle_seconds)
		return MigrationService.create(
			self.virtual_machine,
			destination_metal_server=destination if destination != self.virtual_machine.server else None,
			resize=target_shape,
		)

	@staticmethod
	def _validate_values(resize_values: dict[str, int]) -> None:
		"""Reject an unknown field or a value outside its range before a host read."""
		unknown = set(resize_values) - set(RESIZE_FIELDS)
		if unknown:
			frappe.throw(_("Unknown resize field: {0}.").format(sorted(unknown)[0]), exc=AtlasUserError)
		if any(not isinstance(value, int) or isinstance(value, bool) for value in resize_values.values()):
			frappe.throw(_("Resize values must be integers."), exc=AtlasUserError)

		cpu_millicores = resize_values.get("cpu_millicores")
		if cpu_millicores is not None and not (
			MINIMUM_CPU_MILLICORES <= cpu_millicores <= MAXIMUM_CPU_MILLICORES
		):
			frappe.throw(
				_("CPU must be between {0} and {1} millicores.").format(
					MINIMUM_CPU_MILLICORES, MAXIMUM_CPU_MILLICORES
				),
				exc=AtlasUserError,
			)
		if resize_values.get("memory_mib", 1) <= 0:
			frappe.throw(_("Memory must be positive."), exc=AtlasUserError)
		if resize_values.get("disk_mib", 1) <= 0:
			frappe.throw(_("Disk size must be positive."), exc=AtlasUserError)
		if not 0 <= resize_values.get("sleep_after_idle_seconds", 0) <= MAXIMUM_SLEEP_AFTER_IDLE_SECONDS:
			frappe.throw(_("Idle shutdown delay is out of range."), exc=AtlasUserError)

	@staticmethod
	def _get_current_shape(information: MetalVirtualMachine) -> VirtualMachineShape:
		"""Return the shape Metal stores for the VM."""
		compute = information.desired.compute
		return VirtualMachineShape(
			compute.cpu_millicores,
			compute.memory_mib,
			information.desired.disk.size_mib,
			compute.sleep_after_idle_seconds,
		)

	@staticmethod
	def _get_target_shape(
		current_shape: VirtualMachineShape, resize_values: dict[str, int]
	) -> VirtualMachineShape:
		"""Return the current shape with the changed values. A disk only grows."""
		target_shape = VirtualMachineShape(
			resize_values.get("cpu_millicores", current_shape.cpu_millicores),
			resize_values.get("memory_mib", current_shape.memory_mib),
			resize_values.get("disk_mib", current_shape.disk_mib),
			resize_values.get("sleep_after_idle_seconds", current_shape.sleep_after_idle_seconds),
		)
		if target_shape.disk_mib < current_shape.disk_mib:
			frappe.throw(_("Disk size can only increase."), exc=AtlasUserError)
		return target_shape

	def _get_requirements(self, target: VirtualMachineShape) -> PlacementRequirements:
		"""Return the placement requirements of the target shape."""
		return PlacementRequirements(
			target.cpu_millicores,
			target.memory_mib,
			target.disk_mib,
			cast(str, self.virtual_machine.architecture),
			self.virtual_machine.tenant_id,
			target.sleep_after_idle_seconds > 0,
		)

	def _set_idle_shutdown(self, current: VirtualMachineShape, sleep_after_idle_seconds: int) -> None:
		"""Change only the idle shutdown delay on the current host."""
		self.service.set_compute({**current.compute, "sleep_after_idle_seconds": sleep_after_idle_seconds})
		self.virtual_machine.db_set("sleep_after_idle_seconds", sleep_after_idle_seconds)

	def _sync_shape(self, shape: VirtualMachineShape) -> None:
		"""Store Metal values when a previous request response was lost."""
		values = {
			"cpu_millicores": shape.cpu_millicores,
			"memory_mib": shape.memory_mib,
			"disk_mib": shape.disk_mib,
			"sleep_after_idle_seconds": shape.sleep_after_idle_seconds,
		}
		if any(getattr(self.virtual_machine, field) != value for field, value in values.items()):
			self.virtual_machine.db_set(values)

	def _resize_in_place(self, target: VirtualMachineShape) -> None:
		"""Store the complete target shape on the current host."""
		name = cast(str, self.virtual_machine.name)
		self.service.metal_client.resize_virtual_machine(name, target.resize)
		self.virtual_machine.db_set(
			{
				"cpu_millicores": target.cpu_millicores,
				"memory_mib": target.memory_mib,
				"disk_mib": target.disk_mib,
				"sleep_after_idle_seconds": target.sleep_after_idle_seconds,
			}
		)
