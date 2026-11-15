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
	CPUBurst,
	VirtualMachineShape,
	validate_sleep_times,
)
from atlas.vm.core.placement import AffinityRules, CurrentPlacement, PlacementRequirements, PlacementStrategy
from atlas.vm.core.placement.transaction import use_read_committed
from atlas.vm.core.vm_migration import MigrationService
from atlas.vm.core.vm_service import VirtualMachineService

if TYPE_CHECKING:
	from atlas.vm.core.metal_models import MetalVirtualMachine
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine

# The offload delays allow deep sleep and coma only up to a VM size.
SLEEP_LIMIT_FIELDS = ("deep_sleep_after_idle_seconds", "coma_after_idle_seconds", "memory_mib", "disk_mib")
RESIZE_FIELDS = (
	"cpu_millicores",
	"memory_mib",
	"disk_mib",
	"sleep_after_idle_seconds",
	"deep_sleep_after_idle_seconds",
	"coma_after_idle_seconds",
	"is_burstable",
	"baseline_cpu_millicores",
	"sliding_window_minutes",
)


class VirtualMachineResize:
	"""Resize one VM on its host, or move it to a host that holds the new shape."""

	def __init__(self, virtual_machine: VirtualMachine) -> None:
		self.virtual_machine = virtual_machine
		self.service = VirtualMachineService(virtual_machine)

	@use_read_committed
	def apply(self, resize_values: dict[str, int]) -> str | None:
		"""Apply the changed values. Return the migration name when the VM moves."""
		self._validate_values(resize_values)
		current = {field: getattr(self.virtual_machine, field) or 0 for field in SLEEP_LIMIT_FIELDS}
		self._validate_sleep_times({**current, **resize_values})
		sleep_values = {
			field: resize_values[field]
			for field in ("deep_sleep_after_idle_seconds", "coma_after_idle_seconds")
			if field in resize_values
		}
		shape_values = {field: value for field, value in resize_values.items() if field not in sleep_values}
		migration = self._resize(shape_values) if shape_values or not sleep_values else None
		if sleep_values:
			self._set_sleep_times(sleep_values)
		return migration

	def _resize(self, resize_values: dict[str, int]) -> str | None:
		"""Apply resource, idle shutdown, and CPU burst changes."""
		information = self.service.require_information()
		current_shape = self._get_current_shape(information)
		target_shape = self._get_target_shape(current_shape, resize_values)
		if target_shape.has_same_resources(current_shape):
			self._sync_shape(current_shape)
			if target_shape != current_shape:
				self._set_compute_options(current_shape, target_shape)
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

		# The idle delay and CPU burst need no capacity, so they change on the source and the move keeps them.
		if (target_shape.sleep_after_idle_seconds, target_shape.burst) != (
			current_shape.sleep_after_idle_seconds,
			current_shape.burst,
		):
			self._set_compute_options(current_shape, target_shape)
		return MigrationService.create(
			self.virtual_machine,
			destination_metal_server=destination if destination != self.virtual_machine.server else None,
			resize=target_shape,
		)

	def _set_sleep_times(self, changes: dict[str, int]) -> None:
		"""Merge partial offload changes under a short VM row lock after host work."""
		values = frappe.db.get_value(
			"Virtual Machine", self.virtual_machine.name, SLEEP_LIMIT_FIELDS, as_dict=True, for_update=True
		)
		if values is None:
			frappe.throw(_("Virtual Machine no longer exists."), exc=frappe.DoesNotExistError)
		values.update(changes)
		self._validate_sleep_times(values)
		self.virtual_machine.db_set(changes)

	@staticmethod
	def _validate_sleep_times(values: dict[str, int]) -> None:
		try:
			validate_sleep_times(
				values["deep_sleep_after_idle_seconds"],
				values["coma_after_idle_seconds"],
				values["memory_mib"],
				values["disk_mib"],
			)
		except ValueError as error:
			frappe.throw(_(str(error)), exc=AtlasUserError)

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
		if resize_values.get("is_burstable", 0) not in (0, 1):
			frappe.throw(_("Burstable must be 0 or 1."), exc=AtlasUserError)

	@staticmethod
	def _get_current_shape(information: MetalVirtualMachine) -> VirtualMachineShape:
		"""Return the shape Metal stores for the VM."""
		compute = information.desired.compute
		return VirtualMachineShape(
			compute.cpu_millicores,
			compute.memory_mib,
			information.desired.disk.size_mib,
			compute.sleep_after_idle_seconds,
			CPUBurst(compute.is_burstable, compute.baseline_cpu_millicores, compute.sliding_window_minutes),
		)

	@staticmethod
	def _get_target_shape(
		current_shape: VirtualMachineShape, resize_values: dict[str, int]
	) -> VirtualMachineShape:
		"""Return the current shape with the changed values. A disk only grows."""
		cpu_millicores = resize_values.get("cpu_millicores", current_shape.cpu_millicores)
		burst = current_shape.burst
		try:
			burst = CPUBurst.for_cpu(
				bool(resize_values.get("is_burstable", burst.is_burstable)),
				resize_values.get("baseline_cpu_millicores", burst.baseline_cpu_millicores),
				resize_values.get("sliding_window_minutes", burst.sliding_window_minutes),
				cpu_millicores,
			)
		except ValueError as error:
			frappe.throw(_(str(error)), exc=AtlasUserError)

		target_shape = VirtualMachineShape(
			cpu_millicores,
			resize_values.get("memory_mib", current_shape.memory_mib),
			resize_values.get("disk_mib", current_shape.disk_mib),
			resize_values.get("sleep_after_idle_seconds", current_shape.sleep_after_idle_seconds),
			burst,
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
			placement_rules=AffinityRules.from_json(self.virtual_machine.placement_rules),
			virtual_machine=self.virtual_machine.name,
		)

	def _set_compute_options(self, current: VirtualMachineShape, target: VirtualMachineShape) -> None:
		"""Change only the idle shutdown delay and CPU burst on the current host."""
		if target.burst.baseline_cpu_millicores > current.cpu_millicores:
			frappe.throw(
				_("The baseline CPU must fit the current CPU while the Virtual Machine moves. Resize first."),
				exc=AtlasUserError,
			)
		options = {"sleep_after_idle_seconds": target.sleep_after_idle_seconds, **target.burst.values}
		self.service.set_compute({**current.compute, **options})
		self.virtual_machine.db_set(options)

	def _sync_shape(self, shape: VirtualMachineShape) -> None:
		"""Store Metal values when a previous request response was lost."""
		if any(getattr(self.virtual_machine, field) != value for field, value in shape.resize.items()):
			self.virtual_machine.db_set(shape.resize)

	def _resize_in_place(self, target: VirtualMachineShape) -> None:
		"""Store the complete target shape on the current host."""
		name = cast(str, self.virtual_machine.name)
		self.service.metal_client.resize_virtual_machine(name, target.resize)
		self.virtual_machine.db_set(target.resize)
