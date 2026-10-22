"""Event-driven capacity and money ledger for placement strategies."""

from __future__ import annotations

import heapq
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from math import log

from atlas.simulator.vm_placement.workload import (
	TICKS_PER_DAY,
	TICKS_PER_SECOND,
	ExternalEvent,
	HostType,
	Scenario,
	TenantProfile,
	VMShape,
	Workload,
	event_random,
)
from atlas.vm.core.placement import PlacementRequirements, PlacementStrategy
from atlas.vm.core.placement.models import FleetUsage, HostUsage, Resources

SAMPLE_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
PAID_STATES = frozenset(("running", "asleep", "saving"))


@dataclass(slots=True)
class _Host:
	name: str
	host_type: HostType
	ready: bool
	is_sleepy: bool = False
	allocations: dict[int, Resources] = field(default_factory=dict)
	used_cpu_millicores: int = 0
	used_memory_mib: int = 0
	used_storage_mib: int = 0


@dataclass(slots=True)
class _Migration:
	target_host_name: str
	target_shape_name: str
	target_disk_mib: int
	source_state: str
	action: str


@dataclass(slots=True)
class _VM:
	identifier: int
	tenant_id: int
	shape_name: str
	disk_mib: int
	created_tick: int
	host_name: str
	state: str = "booting"
	last_traffic_tick: int = 0
	sleep_version: int = 0
	lifetime_version: int = 0
	visit_count: int = 0
	visit_generation: int = 0
	active_sessions: set[int] = field(default_factory=set)
	migration: _Migration | None = None


@dataclass(slots=True)
class _Tenant:
	profile: TenantProfile
	virtual_machines: set[int] = field(default_factory=set)
	incidents: int = 0
	last_incident_tick: int = -1
	allocation_multiplier: float = 1.0
	left: bool = False


@dataclass(frozen=True, slots=True)
class _PendingAllocation:
	identifier: int
	tenant_id: int
	shape_name: str
	disk_mib: int


@dataclass(frozen=True, slots=True)
class Result:
	strategy: str
	tenants: int
	tenants_left: int
	vms_created: int
	vms_terminated: int
	running_at_end: int
	asleep_at_end: int
	stopped_at_end: int
	hosts_at_end: int
	regular_hosts_at_end: int
	sleepy_hosts_at_end: int
	new_hosts: int
	host_hours: float
	host_cost_rupees: float
	vm_revenue_rupees: float
	margin_rupees: float
	create_failures: int
	pending_creates: int
	resize_failures: int
	start_failures: int
	wake_failures: int
	incidents: int
	resize_migrations: int
	start_migrations: int
	wake_migrations: int
	sleeps: int
	wakes: int
	cpu_allocation: float
	memory_utilization: float
	storage_utilization: float
	hours_below_three_ready: float


class SimulatedPlacementContext:
	"""Expose only placement inputs and checked actions to a strategy."""

	def __init__(
		self,
		simulation: Simulation,
		requirements: PlacementRequirements,
		action: str,
		current_host_name: str | None,
		virtual_machine_id: int | None,
	) -> None:
		self._simulation = simulation
		self._virtual_machine_id = virtual_machine_id
		self._selected_host_name: str | None = None
		self._pending_host_names: tuple[str, ...] = ()
		self.requirements = requirements
		self.action = action
		self.current_host_name = current_host_name
		self.usage = simulation._usage()
		self.sleepy_vm_overcommit_factor = simulation.scenario.sleepy_vm_overcommit_factor
		self.use_dedicated_sleepy_vm_hosts = True
		self.last_probe_was_contended = False

	@property
	def remaining_seconds(self) -> float:
		"""A simulated placement has no wall clock deadline."""
		return math.inf

	def try_select(self, host_name: str, *, wait: bool = False) -> bool:
		"""Select a simulated host when it still has enough capacity."""
		if self._selected_host_name is not None:
			raise RuntimeError("A placement host is already selected.")
		if host_name not in {host.name for host in self.usage.hosts}:
			return False
		host = self._simulation._hosts[host_name]
		replacing = self._virtual_machine_id if host_name == self.current_host_name else None
		wanted = Resources(
			self.requirements.cpu_millicores,
			self.requirements.memory_mib,
			self.requirements.disk_mib,
		)
		if host.host_type.architecture != self.requirements.architecture or not self._simulation._fits(
			host, wanted, replacing
		):
			return False
		self._selected_host_name = host_name
		return True

	def spawn_host(
		self, host_type: str | None = None, *, count: int = 1, is_sleepy: bool = False
	) -> tuple[str, ...]:
		if isinstance(count, bool) or not isinstance(count, int) or count < 1:
			raise ValueError("count must be a positive integer")
		selected_type = host_type or self._simulation.scenario.new_host_type
		catalog = self._simulation.scenario.host_type(selected_type)
		if (
			catalog.architecture != self.requirements.architecture
			or int(catalog.cpu_millicores * self._simulation.scenario.cpu_oversubscription)
			< self.requirements.cpu_millicores
			or catalog.memory_mib < self.requirements.memory_mib
			or catalog.storage_mib < self.requirements.disk_mib
		):
			raise ValueError(f"Host type {selected_type} cannot hold this VM shape")
		names = [
			host.name
			for host in self._simulation._hosts.values()
			if not host.ready and host.host_type.name == selected_type and host.is_sleepy == is_sleepy
		]
		for _ in range(max(0, count - len(names))):
			if len(self._simulation._hosts) >= self._simulation.scenario.max_host_count:
				break
			names.append(self._simulation._spawn(catalog, is_sleepy=is_sleepy))
		self._pending_host_names = tuple(names)
		return self._pending_host_names


class Simulation:
	"""Own all mutable state for one strategy replay."""

	def __init__(self, scenario: Scenario, workload: Workload, days: int) -> None:
		self.scenario = scenario
		self.workload = workload
		self._horizon = days * TICKS_PER_DAY
		self._now = 0
		self._hosts: dict[str, _Host] = {}
		self._virtual_machines: dict[int, _VM] = {}
		self._pending_allocations: dict[int, _PendingAllocation] = {}
		self._tenants = {profile.identifier: _Tenant(profile) for profile in workload.tenants}
		self._events: list[tuple[int, int, str, object]] = []
		self._sequence = count()
		self._strategy: type[PlacementStrategy] | Callable[[SimulatedPlacementContext], None] | None = None
		self._ready_capacity = Resources(0, 0, 0)
		self._ready_used = Resources(0, 0, 0)
		self._capacity_area = Resources(0, 0, 0)
		self._used_area = Resources(0, 0, 0)
		self._paid_memory_mib = 0
		self._host_monthly_price_sum = 0.0
		self._host_hours_ticks = 0
		self._below_three_ready_ticks = 0
		self._host_cost_rupees = 0.0
		self._vm_revenue_rupees = 0.0
		self._created = 0
		self._terminated = 0
		self._failures = {action: 0 for action in ("create", "resize", "start", "wake")}
		self._migrations = {action: 0 for action in ("resize", "start", "wake")}
		self._sleeps = 0
		self._wakes = 0
		for _ in range(scenario.initial_host_count):
			self._add_host(scenario.host_type(scenario.initial_host_type), ready=True)
		for event in workload.events:
			self._schedule(event.at_tick, "external", event)

	def _schedule(self, at_tick: int, kind: str, payload: object) -> None:
		if at_tick < self._horizon:
			heapq.heappush(self._events, (at_tick, next(self._sequence), kind, payload))

	def _host_total(self, host: _Host) -> Resources:
		return Resources(
			int(host.host_type.cpu_millicores * self.scenario.cpu_oversubscription),
			host.host_type.memory_mib,
			host.host_type.storage_mib,
		)

	def _add_host(self, host_type: HostType, *, ready: bool, is_sleepy: bool | None = None) -> str:
		name = f"host-{len(self._hosts) + 1:04d}"
		host = _Host(name, host_type, ready, host_type.is_sleepy if is_sleepy is None else is_sleepy)
		self._hosts[name] = host
		self._host_monthly_price_sum += host_type.monthly_rupees
		if ready:
			self._ready_capacity = _plus(self._ready_capacity, self._host_total(host))
		return name

	def _spawn(self, host_type: HostType, *, is_sleepy: bool) -> str:
		name = self._add_host(host_type, ready=False, is_sleepy=is_sleepy)
		rng = event_random(self.workload.seed, len(self._hosts))
		minutes = rng.uniform(self.scenario.host_start_minutes_min, self.scenario.host_start_minutes_max)
		self._schedule(self._now + max(1, round(minutes * 60 * TICKS_PER_SECOND)), "host_ready", name)
		return name

	def _change_allocation(self, host: _Host, key: int, resources: Resources | None) -> None:
		old = host.allocations.pop(key, Resources(0, 0, 0))
		new = resources or Resources(0, 0, 0)
		if resources is not None:
			host.allocations[key] = resources
		delta = Resources(
			new.cpu_millicores - old.cpu_millicores,
			new.memory_mib - old.memory_mib,
			new.storage_mib - old.storage_mib,
		)
		host.used_cpu_millicores += delta.cpu_millicores
		host.used_memory_mib += delta.memory_mib
		host.used_storage_mib += delta.storage_mib
		if host.ready:
			self._ready_used = _plus(self._ready_used, delta)

	def _fits(self, host: _Host, wanted: Resources, replacing: int | None = None) -> bool:
		if not host.ready:
			return False
		old = host.allocations.get(replacing, Resources(0, 0, 0))
		total = self._host_total(host)
		return (
			host.used_cpu_millicores - old.cpu_millicores + wanted.cpu_millicores <= total.cpu_millicores
			and host.used_memory_mib - old.memory_mib + wanted.memory_mib <= total.memory_mib
			and host.used_storage_mib - old.storage_mib + wanted.storage_mib <= total.storage_mib
		)

	def _usage(self) -> FleetUsage:
		at = SAMPLE_EPOCH + timedelta(seconds=self._now / TICKS_PER_SECOND)
		hosts = tuple(
			HostUsage(
				name=host.name,
				architecture=host.host_type.architecture,
				is_sleepy=host.is_sleepy,
				sample_created_at=at,
				total=self._host_total(host),
				free=Resources(
					max(self._host_total(host).cpu_millicores - host.used_cpu_millicores, 0),
					max(host.host_type.memory_mib - host.used_memory_mib, 0),
					max(host.host_type.storage_mib - host.used_storage_mib, 0),
				),
				sleepy_reserved_memory_mib=sum(
					self.scenario.shape(self._virtual_machines[key].shape_name).memory_mib
					for key in host.allocations
					if key > 0 and self.scenario.shape(self._virtual_machines[key].shape_name).is_sleepy
				),
			)
			for host in self._hosts.values()
			if host.ready
		)
		return FleetUsage(
			hosts,
			self._ready_capacity,
			Resources(
				sum(host.free.cpu_millicores for host in hosts),
				sum(host.free.memory_mib for host in hosts),
				sum(host.free.storage_mib for host in hosts),
			),
			sum(host.sleepy_reserved_memory_mib for host in hosts),
		)

	def _choose(
		self, requirements: PlacementRequirements, action: str, vm: _VM | None
	) -> tuple[str | None, tuple[str, ...]]:
		placement = SimulatedPlacementContext(
			self, requirements, action, vm.host_name if vm else None, vm.identifier if vm else None
		)
		if isinstance(self._strategy, type):
			self._strategy(placement).select_host()
		else:
			self._strategy(placement)
		if placement._selected_host_name is None and not placement._pending_host_names:
			placement.spawn_host(is_sleepy=requirements.is_sleepy)
		return placement._selected_host_name, placement._pending_host_names

	def _placement_requirements(self, tenant_id: int, shape: VMShape, disk_mib: int) -> PlacementRequirements:
		return PlacementRequirements(
			shape.cpu_millicores,
			shape.memory_mib,
			disk_mib,
			self.scenario.host_type(self.scenario.initial_host_type).architecture,
			tenant_id,
			shape.is_sleepy,
		)

	def _disk_mib(self, shape: VMShape, rng: random.Random) -> int:
		if not shape.is_sleepy:
			return shape.disk_mib
		traffic = self.scenario.traffic
		return max(
			1,
			round(
				shape.disk_mib
				* rng.uniform(traffic.thin_storage_fraction_min, traffic.thin_storage_fraction_max)
			),
		)

	def _paid(self, vm: _VM) -> int:
		if vm.state in PAID_STATES or (
			vm.state == "migrating" and vm.migration and vm.migration.source_state in PAID_STATES
		):
			return self.scenario.shape(vm.shape_name).memory_mib
		return 0

	def _schedule_lifetime(self, vm: _VM) -> None:
		vm.lifetime_version += 1
		shape = self.scenario.shape(vm.shape_name)
		median = (
			self.scenario.traffic.sleepy_lifetime_median_days
			if shape.is_sleepy
			else self.scenario.traffic.regular_lifetime_median_days
		)
		rng = event_random(self.workload.seed, vm.identifier + vm.lifetime_version * 97)
		elapsed = max(
			1, round(rng.lognormvariate(log(median), self.scenario.traffic.lifetime_sigma) * TICKS_PER_DAY)
		)
		self._schedule(self._now + elapsed, "lifetime", (vm.identifier, vm.lifetime_version))

	def _schedule_visits(self, vm: _VM) -> None:
		if not self.scenario.shape(vm.shape_name).is_sleepy:
			return
		vm.visit_count += 1
		rng = event_random(self.workload.seed, vm.identifier + vm.visit_count * 73)
		at = self._now + max(1, round(rng.expovariate(self.scenario.traffic.visits_per_day / TICKS_PER_DAY)))
		duration = max(
			1,
			round(
				rng.lognormvariate(
					log(self.scenario.traffic.session_median_minutes), self.scenario.traffic.session_sigma
				)
				* 60
				* TICKS_PER_SECOND
			),
		)
		self._schedule(at, "visit", (vm.identifier, vm.identifier + at, duration, True, vm.visit_generation))

	def _allocate(self, tenant: _Tenant, event: ExternalEvent, rng: random.Random) -> None:
		for index in range(event.count):
			pending_count = sum(
				allocation.tenant_id == tenant.profile.identifier
				for allocation in self._pending_allocations.values()
			)
			if tenant.left or len(tenant.virtual_machines) + pending_count >= tenant.profile.target_size:
				return
			if rng.random() >= tenant.allocation_multiplier:
				continue
			shape = rng.choices(self.scenario.shapes, weights=tenant.profile.shape_weights)[0]
			disk_mib = self._disk_mib(shape, rng)
			allocation = _PendingAllocation(
				event.identifier * 1_000_000 + index + 1,
				tenant.profile.identifier,
				shape.name,
				disk_mib,
			)
			if self._place_allocation(allocation) is False:
				return

	def _place_allocation(self, allocation: _PendingAllocation) -> bool | None:
		"""Place one request, wait for simulated capacity, or record a hard failure."""
		tenant = self._tenants[allocation.tenant_id]
		shape = self.scenario.shape(allocation.shape_name)
		requirements = self._placement_requirements(allocation.tenant_id, shape, allocation.disk_mib)
		host_name, pending_hosts = self._choose(requirements, "create", None)
		if host_name is None:
			if pending_hosts:
				self._pending_allocations[allocation.identifier] = allocation
				return None
			self._pending_allocations.pop(allocation.identifier, None)
			self._failure(tenant, "create")
			return False

		self._pending_allocations.pop(allocation.identifier, None)
		vm_id = allocation.identifier
		vm = _VM(
			vm_id,
			allocation.tenant_id,
			shape.name,
			allocation.disk_mib,
			self._now,
			host_name,
			last_traffic_tick=self._now,
		)
		self._virtual_machines[vm_id] = vm
		tenant.virtual_machines.add(vm_id)
		self._change_allocation(
			self._hosts[host_name],
			vm_id,
			Resources(shape.cpu_millicores, shape.memory_mib, allocation.disk_mib),
		)
		self._created += 1
		self._schedule(
			self._now + max(1, round(self.scenario.traffic.boot_seconds * TICKS_PER_SECOND)),
			"boot",
			vm_id,
		)
		self._schedule_lifetime(vm)
		self._schedule_visits(vm)
		return True

	def _retry_pending_allocations(self) -> None:
		for allocation in tuple(self._pending_allocations.values()):
			tenant = self._tenants[allocation.tenant_id]
			if tenant.left or len(tenant.virtual_machines) >= tenant.profile.target_size:
				self._pending_allocations.pop(allocation.identifier)
				continue
			self._place_allocation(allocation)

	def _terminate(self, vm: _VM) -> None:
		if vm.state == "terminated":
			return
		self._paid_memory_mib -= self._paid(vm)
		self._change_allocation(self._hosts[vm.host_name], vm.identifier, None)
		if vm.migration is not None:
			self._change_allocation(self._hosts[vm.migration.target_host_name], -vm.identifier, None)
		self._tenants[vm.tenant_id].virtual_machines.discard(vm.identifier)
		vm.state = "terminated"
		vm.migration = None
		vm.active_sessions.clear()
		self._terminated += 1

	def _failure(self, tenant: _Tenant, action: str) -> None:
		self._failures[action] += 1
		limit = round(self.scenario.incidents.group_seconds * TICKS_PER_SECOND)
		if tenant.last_incident_tick >= 0 and self._now - tenant.last_incident_tick <= limit:
			return
		tenant.incidents += 1
		tenant.last_incident_tick = self._now
		tenant.allocation_multiplier *= self.scenario.incidents.allocation_multiplier
		rng = event_random(self.workload.seed, tenant.profile.identifier * 1_000_000 + self._now)
		fraction = rng.uniform(
			self.scenario.incidents.terminate_fraction_min, self.scenario.incidents.terminate_fraction_max
		)
		victims = (
			rng.sample(
				sorted(tenant.virtual_machines),
				min(len(tenant.virtual_machines), max(1, round(len(tenant.virtual_machines) * fraction))),
			)
			if tenant.virtual_machines
			else []
		)
		for vm_id in victims:
			self._terminate(self._virtual_machines[vm_id])
		if tenant.incidents >= 2 and rng.random() < min(
			1, self.scenario.incidents.leave_probability_step * (tenant.incidents - 1)
		):
			tenant.left = True
			for vm_id in tuple(tenant.virtual_machines):
				self._terminate(self._virtual_machines[vm_id])

	def _stop(self, vm: _VM) -> None:
		if vm.state not in PAID_STATES:
			return
		self._paid_memory_mib -= self._paid(vm)
		self._change_allocation(self._hosts[vm.host_name], vm.identifier, Resources(0, 0, vm.disk_mib))
		vm.state = "stopped"
		vm.sleep_version += 1
		vm.active_sessions.clear()

	def _start(self, vm: _VM) -> None:
		if vm.state != "stopped":
			return
		shape = self.scenario.shape(vm.shape_name)
		requirements = self._placement_requirements(vm.tenant_id, shape, vm.disk_mib)
		host_name, _ = self._choose(requirements, "start", vm)
		if host_name is None:
			self._failure(self._tenants[vm.tenant_id], "start")
			return
		wanted = Resources(shape.cpu_millicores, shape.memory_mib, vm.disk_mib)
		if host_name == vm.host_name:
			self._change_allocation(self._hosts[host_name], vm.identifier, wanted)
			vm.state = "booting"
			self._schedule(
				self._now + max(1, round(self.scenario.traffic.boot_seconds * TICKS_PER_SECOND)),
				"boot",
				vm.identifier,
			)
		else:
			self._begin_migration(vm, host_name, shape, vm.disk_mib, "start")

	def _resize(self, vm: _VM, shape: VMShape, disk_mib: int) -> bool:
		if vm.state not in (*PAID_STATES, "stopped") or shape.name == vm.shape_name:
			return False
		requirements = self._placement_requirements(vm.tenant_id, shape, disk_mib)
		host_name, _ = self._choose(requirements, "resize", vm)
		if host_name is None:
			self._failure(self._tenants[vm.tenant_id], "resize")
			return False
		wanted = Resources(shape.cpu_millicores, shape.memory_mib, disk_mib)
		if host_name == vm.host_name:
			before = self._paid(vm)
			was_stopped = vm.state == "stopped"
			self._change_allocation(
				self._hosts[host_name], vm.identifier, Resources(0, 0, disk_mib) if was_stopped else wanted
			)
			vm.shape_name = shape.name
			vm.visit_generation += 1
			vm.disk_mib = disk_mib
			vm.state = "stopped" if was_stopped else "running"
			vm.sleep_version += 1
			self._paid_memory_mib += self._paid(vm) - before
			self._schedule_lifetime(vm)
			self._schedule_visits(vm)
			self._schedule_sleep(vm)
		else:
			self._begin_migration(vm, host_name, shape, disk_mib, "resize")
		return True

	def _begin_migration(self, vm: _VM, host_name: str, shape: VMShape, disk_mib: int, action: str) -> None:
		self._change_allocation(
			self._hosts[host_name],
			-vm.identifier,
			Resources(shape.cpu_millicores, shape.memory_mib, disk_mib),
		)
		vm.migration = _Migration(host_name, shape.name, disk_mib, vm.state, action)
		vm.state = "migrating"
		vm.sleep_version += 1
		if action == "wake":
			rng = event_random(self.workload.seed, vm.identifier + self._now)
			seconds = rng.uniform(
				self.scenario.traffic.wake_migration_seconds_min,
				self.scenario.traffic.wake_migration_seconds_max,
			)
		else:
			seconds = (
				self.scenario.traffic.migration_seconds_per_gib
				* self.scenario.shape(vm.shape_name).memory_mib
				/ 1024
			)
		self._schedule(self._now + max(1, round(seconds * TICKS_PER_SECOND)), "migration_done", vm.identifier)

	def _finish_migration(self, vm: _VM) -> None:
		if vm.state != "migrating" or vm.migration is None:
			return
		migration = vm.migration
		before = self._paid(vm)
		self._change_allocation(self._hosts[vm.host_name], vm.identifier, None)
		target = self._hosts[migration.target_host_name]
		self._change_allocation(target, -vm.identifier, None)
		new_shape = self.scenario.shape(migration.target_shape_name)
		self._change_allocation(
			target,
			vm.identifier,
			Resources(0, 0, migration.target_disk_mib)
			if migration.action == "resize" and migration.source_state == "stopped"
			else Resources(new_shape.cpu_millicores, new_shape.memory_mib, migration.target_disk_mib),
		)
		vm.host_name = migration.target_host_name
		vm.shape_name = new_shape.name
		if migration.action == "resize":
			vm.visit_generation += 1
		vm.disk_mib = migration.target_disk_mib
		vm.migration = None
		vm.state = (
			"booting"
			if migration.action == "start"
			else "stopped"
			if migration.action == "resize" and migration.source_state == "stopped"
			else "running"
		)
		self._paid_memory_mib += self._paid(vm) - before
		self._migrations[migration.action] += 1
		if migration.action == "start":
			self._schedule(
				self._now + max(1, round(self.scenario.traffic.boot_seconds * TICKS_PER_SECOND)),
				"boot",
				vm.identifier,
			)
		else:
			if migration.action == "resize":
				self._schedule_lifetime(vm)
				self._schedule_visits(vm)
			self._schedule_sleep(vm)

	def _wake(self, vm: _VM) -> None:
		if vm.state != "asleep":
			return
		shape = self.scenario.shape(vm.shape_name)
		wanted = Resources(shape.cpu_millicores, shape.memory_mib, vm.disk_mib)
		current = self._hosts[vm.host_name]
		if self._fits(current, wanted, vm.identifier):
			self._change_allocation(current, vm.identifier, wanted)
			vm.state = "running"
			self._wakes += 1
			self._schedule_sleep(vm)
			return
		requirements = self._placement_requirements(vm.tenant_id, shape, vm.disk_mib)
		target, _ = self._choose(requirements, "wake", vm)
		if target is None:
			self._failure(self._tenants[vm.tenant_id], "wake")
			return
		self._begin_migration(vm, target, shape, vm.disk_mib, "wake")
		self._wakes += 1

	def _schedule_sleep(self, vm: _VM) -> None:
		if vm.state != "running" or not self.scenario.shape(vm.shape_name).is_sleepy or vm.active_sessions:
			return
		vm.sleep_version += 1
		idle_ticks = round(self.scenario.traffic.idle_minutes * 60 * TICKS_PER_SECOND)
		at = max(vm.created_tick + idle_ticks, vm.last_traffic_tick + idle_ticks, self._now + 1)
		self._schedule(at, "sleep_begin", (vm.identifier, vm.sleep_version))

	def _visit(self, vm_id: int, session_id: int, duration: int, is_periodic: bool, generation: int) -> None:
		vm = self._virtual_machines.get(vm_id)
		if is_periodic and (vm is None or vm.visit_generation != generation):
			return
		if is_periodic and vm is not None and vm.state != "terminated":
			self._schedule_visits(vm)
		if (
			vm is None
			or not self.scenario.shape(vm.shape_name).is_sleepy
			or vm.state in ("terminated", "stopped")
		):
			return
		vm.active_sessions.add(session_id)
		vm.last_traffic_tick = self._now
		vm.sleep_version += 1
		if vm.state == "saving":
			vm.state = "running"
		elif vm.state == "asleep":
			self._wake(vm)
		self._schedule(self._now + duration, "visit_end", (vm_id, session_id))

	def _visit_end(self, vm_id: int, session_id: int) -> None:
		vm = self._virtual_machines.get(vm_id)
		if vm is None or session_id not in vm.active_sessions:
			return
		vm.active_sessions.remove(session_id)
		vm.last_traffic_tick = self._now
		self._schedule_sleep(vm)

	def _traffic_wave(self, event: ExternalEvent) -> None:
		rng = event_random(self.workload.seed, event.identifier)
		eligible = [
			vm
			for vm in sorted(self._virtual_machines.values(), key=lambda vm: vm.identifier)
			if self.scenario.shape(vm.shape_name).is_sleepy and vm.state in ("running", "asleep", "saving")
		]
		count = min(len(eligible), round(len(eligible) * event.fraction))
		for index, vm in enumerate(rng.sample(eligible, count)):
			duration = max(
				1,
				round(
					rng.lognormvariate(
						log(self.scenario.traffic.session_median_minutes), self.scenario.traffic.session_sigma
					)
					* 60
					* TICKS_PER_SECOND
				),
			)
			self._schedule(
				event.at_tick + rng.randrange(max(1, event.span_ticks)),
				"visit",
				(vm.identifier, event.identifier * 1_000_000 + index, duration, False, 0),
			)

	def _tenant_action(self, event: ExternalEvent) -> None:
		tenant = self._tenants[event.tenant_id]
		if tenant.left:
			return
		rng = event_random(self.workload.seed, event.identifier)
		if event.kind == "tenant_arrival" or event.action == "allocate":
			self._allocate(tenant, event, rng)
			return
		if event.kind == "night_stop":
			action = "stop"
		elif event.kind == "night_start":
			action = "start"
		else:
			action = event.action
		eligible = [
			self._virtual_machines[vm_id]
			for vm_id in sorted(tenant.virtual_machines)
			if (action == "stop" and self._virtual_machines[vm_id].state in PAID_STATES)
			or (action == "start" and self._virtual_machines[vm_id].state == "stopped")
			or (action == "resize" and self._virtual_machines[vm_id].state in (*PAID_STATES, "stopped"))
			or action == "terminate"
		]
		count = min(
			len(eligible),
			max(1, round(len(eligible) * event.fraction)) if event.kind.startswith("night_") else event.count,
		)
		for vm in rng.sample(eligible, count):
			if tenant.left:
				break
			if action == "stop":
				self._stop(vm)
			elif action == "start":
				self._start(vm)
			elif action == "terminate":
				self._terminate(vm)
			elif action == "resize":
				shapes = [shape for shape in self.scenario.shapes if shape.name != vm.shape_name]
				weights = [
					weight
					for shape, weight in zip(self.scenario.shapes, tenant.profile.shape_weights, strict=True)
					if shape.name != vm.shape_name
				]
				shape = rng.choices(shapes, weights=weights)[0]
				self._resize(vm, shape, self._disk_mib(shape, rng))

	def _lifetime(self, vm_id: int, version: int) -> None:
		vm = self._virtual_machines.get(vm_id)
		if vm is None or vm.state == "terminated" or vm.lifetime_version != version:
			return
		if vm.state == "migrating":
			self._schedule(self._now + 60 * TICKS_PER_SECOND, "lifetime", (vm_id, version))
			return
		shape = self.scenario.shape(vm.shape_name)
		if not shape.is_sleepy:
			self._terminate(vm)
			return
		rng = event_random(self.workload.seed, vm.identifier + version * 101)
		if rng.random() >= self.scenario.traffic.sleepy_upgrade_probability:
			self._terminate(vm)
			return
		upgrade = self.scenario.shape(rng.choice(("a1", "a2")))
		if not self._resize(vm, upgrade, self._disk_mib(upgrade, rng)) and vm.state != "terminated":
			self._schedule(self._now + TICKS_PER_DAY, "lifetime", (vm_id, version))

	def _accrue(self, next_tick: int) -> None:
		elapsed = next_tick - self._now
		if elapsed <= 0:
			return
		monthly_ticks = self.scenario.billing_month_days * TICKS_PER_DAY
		basis = self.scenario.host_type(self.scenario.initial_host_type)
		revenue_per_mib_month = self.scenario.revenue_multiplier * basis.monthly_rupees / basis.memory_mib
		self._host_cost_rupees += self._host_monthly_price_sum * elapsed / monthly_ticks
		self._vm_revenue_rupees += self._paid_memory_mib * revenue_per_mib_month * elapsed / monthly_ticks
		self._host_hours_ticks += len(self._hosts) * elapsed
		ready_regular_below_limit = sum(
			host.ready
			and not host.is_sleepy
			and host.used_memory_mib * 100 < host.host_type.memory_mib * 85
			and host.used_storage_mib * 100 < host.host_type.storage_mib * 85
			for host in self._hosts.values()
		)
		if ready_regular_below_limit < 3:
			self._below_three_ready_ticks += elapsed
		self._capacity_area = _plus(self._capacity_area, _scale(self._ready_capacity, elapsed))
		self._used_area = _plus(self._used_area, _scale(self._ready_used, elapsed))

	def run(
		self,
		name: str,
		strategy: type[PlacementStrategy] | Callable[[SimulatedPlacementContext], None],
	) -> Result:
		self._strategy = strategy
		while self._events:
			at, _, kind, payload = heapq.heappop(self._events)
			self._accrue(at)
			self._now = at
			if kind == "external":
				event = payload
				if event.kind == "traffic_wave":
					self._traffic_wave(event)
				else:
					self._tenant_action(event)
			elif kind == "host_ready":
				host = self._hosts[payload]
				host.ready = True
				self._ready_capacity = _plus(self._ready_capacity, self._host_total(host))
				self._retry_pending_allocations()
			elif kind == "boot":
				vm = self._virtual_machines.get(payload)
				if vm is not None and vm.state == "booting":
					vm.state = "running"
					self._paid_memory_mib += self._paid(vm)
					self._schedule_sleep(vm)
			elif kind == "lifetime":
				self._lifetime(*payload)
			elif kind == "visit":
				self._visit(*payload)
			elif kind == "visit_end":
				self._visit_end(*payload)
			elif kind == "sleep_begin":
				vm_id, version = payload
				vm = self._virtual_machines.get(vm_id)
				if (
					vm is not None
					and vm.state == "running"
					and vm.sleep_version == version
					and not vm.active_sessions
				):
					vm.state = "saving"
					self._schedule(
						self._now + max(1, round(self.scenario.traffic.save_seconds * TICKS_PER_SECOND)),
						"sleep_done",
						payload,
					)
			elif kind == "sleep_done":
				vm_id, version = payload
				vm = self._virtual_machines.get(vm_id)
				if vm is not None and vm.state == "saving" and vm.sleep_version == version:
					self._change_allocation(
						self._hosts[vm.host_name], vm.identifier, Resources(0, 0, vm.disk_mib)
					)
					vm.state = "asleep"
					self._sleeps += 1
			elif kind == "migration_done":
				vm = self._virtual_machines.get(payload)
				if vm is not None:
					self._finish_migration(vm)
		self._accrue(self._horizon)
		states = [vm.state for vm in self._virtual_machines.values()]
		cost = self._host_cost_rupees
		revenue = self._vm_revenue_rupees
		return Result(
			strategy=name,
			tenants=len(self._tenants),
			tenants_left=sum(tenant.left for tenant in self._tenants.values()),
			vms_created=self._created,
			vms_terminated=self._terminated,
			running_at_end=states.count("running"),
			asleep_at_end=states.count("asleep"),
			stopped_at_end=states.count("stopped"),
			hosts_at_end=len(self._hosts),
			regular_hosts_at_end=sum(not host.is_sleepy for host in self._hosts.values()),
			sleepy_hosts_at_end=sum(host.is_sleepy for host in self._hosts.values()),
			new_hosts=len(self._hosts) - self.scenario.initial_host_count,
			host_hours=self._host_hours_ticks / (TICKS_PER_SECOND * 3600),
			host_cost_rupees=cost,
			vm_revenue_rupees=revenue,
			margin_rupees=revenue - cost,
			create_failures=self._failures["create"],
			pending_creates=sum(
				not self._tenants[allocation.tenant_id].left
				for allocation in self._pending_allocations.values()
			),
			resize_failures=self._failures["resize"],
			start_failures=self._failures["start"],
			wake_failures=self._failures["wake"],
			incidents=sum(tenant.incidents for tenant in self._tenants.values()),
			resize_migrations=self._migrations["resize"],
			start_migrations=self._migrations["start"],
			wake_migrations=self._migrations["wake"],
			sleeps=self._sleeps,
			wakes=self._wakes,
			cpu_allocation=_ratio(self._used_area.cpu_millicores, self._capacity_area.cpu_millicores),
			memory_utilization=_ratio(self._used_area.memory_mib, self._capacity_area.memory_mib),
			storage_utilization=_ratio(self._used_area.storage_mib, self._capacity_area.storage_mib),
			hours_below_three_ready=self._below_three_ready_ticks / (TICKS_PER_SECOND * 3600),
		)


def _plus(left: Resources, right: Resources) -> Resources:
	return Resources(
		left.cpu_millicores + right.cpu_millicores,
		left.memory_mib + right.memory_mib,
		left.storage_mib + right.storage_mib,
	)


def _scale(resources: Resources, scale: int) -> Resources:
	return Resources(
		resources.cpu_millicores * scale, resources.memory_mib * scale, resources.storage_mib * scale
	)


def _ratio(used: int, capacity: int) -> float:
	return used / capacity if capacity else 0.0
