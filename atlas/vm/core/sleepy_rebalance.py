from __future__ import annotations

from collections import Counter
from datetime import timedelta
from functools import cached_property

import frappe
from frappe.utils import now_datetime

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.vm.core.metal_client import MetalClient
from atlas.vm.core.metal_models import MetalVirtualMachine
from atlas.vm.core.placement import AffinityRules, PlacementRequirements
from atlas.vm.core.placement.context import CAPACITY_MAXIMUM_AGE, PlacementContext
from atlas.vm.core.placement.models import HostUsage
from atlas.vm.core.placement.transaction import use_read_committed
from atlas.vm.core.vm_migration import MigrationService


class SleepyHostRebalance:
	"""Select VM moves from one memory-pressured sleepy host."""

	def __init__(
		self,
		server_name: str,
		virtual_machines: set[str] | None = None,
		*,
		allowed_destinations: set[str] | None = None,
	) -> None:
		self.server = frappe.get_doc("Metal Server", server_name)
		self.virtual_machines = virtual_machines
		self.allowed_destinations = allowed_destinations
		self.memory_reports: dict[str, frappe._dict | None] = {}
		self._planned_memory: Counter[str] = Counter()
		self._planned_disk: Counter[str] = Counter()

	def memory_report(self, server_name: str) -> frappe._dict | None:
		"""Read fresh capacity and host-matched VM states without guessing missing states."""
		if server_name in self.memory_reports:
			report = self.memory_reports[server_name]
			return report if report and report.creation >= now_datetime() - CAPACITY_MAXIMUM_AGE else None
		cutoff = now_datetime() - CAPACITY_MAXIMUM_AGE
		samples = frappe.get_all(
			"Metal Server Usage",
			filters={"server": server_name, "creation": (">=", cutoff)},
			fields=["creation", "total_memory_mib"],
			order_by="creation desc",
			limit=1,
		)
		machines = frappe.get_all(
			"Virtual Machine",
			filters={"server": server_name, "is_draft": 0},
			fields=["name", "memory_mib"],
		)
		states = {
			row.name: row.status
			for row in frappe.get_all(
				"Virtual Machine State",
				filters={"server": server_name, "synced_at": (">=", cutoff)},
				fields=["name", "status"],
			)
		}
		report = None
		if (
			samples
			and samples[0].total_memory_mib > 0
			and all(states.get(machine.name) in {"running", "paused", "stopped"} for machine in machines)
		):
			report = samples[0]
			report.awake_memory_mib = sum(
				machine.memory_mib for machine in machines if states[machine.name] in {"running", "paused"}
			)
		self.memory_reports[server_name] = report
		return report

	def is_eligible(self, information: MetalVirtualMachine) -> bool:
		"""Recheck sleep state and pressure before an automatic move starts."""
		if (
			self.server.status != "Running"
			or not self.server.is_sleepy_vm_host
			or not self.server.is_provisioning_completed
		):
			return False
		report = self.memory_report(self.server.name)
		if report is None or report.awake_memory_mib * 100 < report.total_memory_mib * 85:
			return False
		if (
			information.desired.state != "running"
			or information.desired.compute.sleep_after_idle_seconds <= 0
		):
			return False
		if information.observed.state == "stopped":
			return information.observed.sleep_state in {"sleeping", "deep-sleeping", "comatose"}
		return (
			information.observed.state == "running"
			and report.awake_memory_mib * 100 > report.total_memory_mib * 90
		)

	@cached_property
	def candidates(self) -> list[frappe._dict]:
		"""Order eligible running VMs by recent moves, then sleeping VMs by memory."""
		report = self.memory_report(self.server.name)
		if not report or report.awake_memory_mib * 100 < report.total_memory_mib * 85:
			return []
		filters = {
			"server": self.server.name,
			"sleep_after_idle_seconds": (">", 0),
			"is_draft": 0,
			"is_terminating": 0,
			"is_termination_protected": 0,
			"active_migration": ("is", "not set"),
		}
		if self.virtual_machines is not None:
			filters["name"] = ("in", list(self.virtual_machines))
		services = self._service_virtual_machines()
		client = MetalClient(self.server)
		candidates = []
		for machine in frappe.get_all(
			"Virtual Machine",
			filters=filters,
			fields=[
				"name",
				"memory_mib",
				"disk_mib",
				"cpu_millicores",
				"tenant_id",
				"placement_rules",
			],
		):
			if machine.name in services:
				continue
			information = client.get_virtual_machine(machine.name)
			if self.is_eligible(information):
				machine.is_running = information.observed.state == "running"
				candidates.append(machine)
		counts = Counter(
			frappe.get_all(
				"Virtual Machine Migration",
				filters={
					"status": "completed",
					"finished_at": (">=", now_datetime() - timedelta(days=1)),
				},
				pluck="virtual_machine",
			)
		)
		return sorted(
			candidates,
			key=lambda machine: (
				not machine.is_running,
				counts[machine.name] if machine.is_running else 0,
				-machine.memory_mib,
				machine.name,
			),
		)

	@use_read_committed
	def destinations(self, machine: frappe._dict) -> list[HostUsage]:
		"""Reuse placement admission inputs and retain emptier compatible sleepy hosts."""
		requirements = PlacementRequirements(
			machine.cpu_millicores,
			machine.memory_mib,
			machine.disk_mib,
			self.server.architecture,
			machine.tenant_id,
			True,
			AffinityRules.from_json(machine.placement_rules),
			machine.name,
		)
		placement = PlacementContext(
			requirements, 1.0, {self.server.name}, allowed_servers=self.allowed_destinations
		)
		checker = MigrationService(
			frappe.get_doc(
				{
					"doctype": "Virtual Machine Migration",
					"source_metal_server": self.server.name,
					"has_memory_snapshot": 1,
				}
			)
		)
		source = self.memory_report(self.server.name)
		targets = []
		if source is None:
			return targets
		for host in placement.usage.hosts:
			report = self.memory_report(host.name)
			if not self._has_room(host, machine, report, source):
				continue
			if frappe.db.exists(
				"Metal Server Drain", {"source_metal_server": host.name, "status": "Running"}
			):
				continue
			try:
				checker.validate_compatibility(host.name, self.server.architecture)
			except AtlasUserError:
				continue
			targets.append(host)
		return sorted(
			targets,
			key=lambda host: (
				self.memory_reports[host.name].awake_memory_mib / host.total.memory_mib,
				host.name,
			),
		)

	def plan(self) -> tuple[list[str], list[str]]:
		"""Fit a batch and select running moves toward 85 percent."""
		selected = []
		destinations: set[str] = set()
		self._planned_memory.clear()
		self._planned_disk.clear()
		source = self.memory_report(self.server.name)
		if source is None:
			return [], []
		awake_memory = source.awake_memory_mib
		for machine in self.candidates:
			if machine.is_running and awake_memory * 100 <= source.total_memory_mib * 85:
				continue
			targets = self.destinations(machine)
			if not targets:
				continue
			selected.append(machine.name)
			destinations.update(host.name for host in targets)
			self._planned_memory[targets[0].name] += machine.memory_mib
			self._planned_disk[targets[0].name] += machine.disk_mib
			if machine.is_running:
				awake_memory -= machine.memory_mib
		return selected, sorted(destinations)

	def _has_room(
		self, host: HostUsage, machine: frappe._dict, report: frappe._dict | None, source: frappe._dict
	) -> bool:
		if not host.is_sleepy or report is None:
			return False
		memory = machine.memory_mib + self._planned_memory[host.name]
		disk = machine.disk_mib + self._planned_disk[host.name]
		used_memory = max(report.awake_memory_mib, host.total.memory_mib - host.free.memory_mib)
		return (
			report.awake_memory_mib * source.total_memory_mib
			< source.awake_memory_mib * host.total.memory_mib
			and (used_memory + memory) * 100 <= host.total.memory_mib * 90
			and host.free.memory_mib >= memory
			and host.free.storage_mib >= disk
		)

	@staticmethod
	def _service_virtual_machines() -> set[str]:
		return {
			name
			for doctype in ("Proxy Server", "Cargo Server", "IPv6 Router Server", "Wireguard Gateway Server")
			if frappe.db.table_exists(doctype)
			for name in frappe.get_all(doctype, pluck="virtual_machine")
			if name
		}
