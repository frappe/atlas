from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import timedelta
from hashlib import sha256
from typing import assert_never

import frappe
from frappe import _
from frappe.utils import now_datetime

from atlas.vm.core.placement.affinity import (
	RESERVED_MIGRATION_STATUSES,
	AffinityUnsatisfied,
	load_affinity_matching,
)
from atlas.vm.core.placement.models import (
	CurrentPlacement,
	FleetUsage,
	HostUsage,
	PlacementRequirements,
	Resources,
)
from atlas.vm.core.placement.transaction import is_read_committed

CAPACITY_MAXIMUM_AGE = timedelta(minutes=2)
PLACEMENT_RATE_WINDOW = timedelta(minutes=5)
# Keep reservations longer than capacity samples.
RESERVATION_MAXIMUM_AGE = timedelta(minutes=15)
# Snapshots rank hosts. Locked reads recheck capacity.
SNAPSHOT_CACHE_SECONDS = 1
# Bound waits for a host lock.
LOCK_WAIT_MAXIMUM_SECONDS = 0.15
# Cache full-pool results briefly.
FULL_POOL_CACHE_SECONDS = 0.5


def _full_pool_key(requirements: PlacementRequirements) -> str:
	return "atlas:placement-full:{0}:{1}".format(requirements.architecture, int(requirements.is_sleepy))


def remember_pool_is_full(requirements: PlacementRequirements) -> None:
	"""Record that no host could hold this shape."""
	frappe.cache().set_value(
		_full_pool_key(requirements),
		{
			"memory_mib": requirements.memory_mib,
			"disk_mib": requirements.disk_mib,
			"until": time.time() + FULL_POOL_CACHE_SECONDS,
		},
		expires_in_sec=1,
	)


def is_pool_known_full(requirements: PlacementRequirements) -> bool:
	"""Return whether this shape is at least as large as a recent failed shape."""
	entry = frappe.cache().get_value(_full_pool_key(requirements))
	if not entry or entry["until"] <= time.time():
		return False

	return requirements.memory_mib >= entry["memory_mib"] and requirements.disk_mib >= entry["disk_mib"]


class PlacementContext:
	"""Own the snapshot and host lock for one placement attempt."""

	def __init__(
		self,
		requirements: PlacementRequirements,
		sleepy_vm_overcommit_factor: float,
		exclude_servers: set[str] | None = None,
		*,
		allowed_servers: set[str] | None = None,
		cache_snapshot: bool = False,
		deadline: float | None = None,
		use_dedicated_sleepy_vm_hosts: bool = True,
		current_placement: CurrentPlacement | None = None,
	) -> None:
		if not is_read_committed():
			raise RuntimeError("Placement needs a READ COMMITTED transaction to recheck committed capacity.")

		self.requirements = requirements
		self.current_placement = current_placement
		if current_placement:
			self.action = "resize"
		else:
			self.action = "migration" if exclude_servers else "create"
		self.current_host_name = current_placement.host_name if current_placement else None
		self.sleepy_vm_overcommit_factor = sleepy_vm_overcommit_factor
		self.use_dedicated_sleepy_vm_hosts = use_dedicated_sleepy_vm_hosts
		# The affinity filter sets this when it removes hosts from the snapshot.
		self.apply_affinity = False
		self.has_contended_hosts = False
		self.probe_count = 0
		self.last_probe_was_contended = False
		self._deadline = deadline
		self._excluded_servers = frozenset(exclude_servers or ())
		self._allowed_servers = frozenset(allowed_servers) if allowed_servers is not None else None
		self._created_at = now_datetime()
		self._selected_host: str | None = None
		self.usage = self._load_fleet_usage(cache_snapshot)

	@property
	def remaining_seconds(self) -> float:
		"""Return the time left for this placement, or the full budget when unbounded."""
		if self._deadline is None:
			return LOCK_WAIT_MAXIMUM_SECONDS
		return self._deadline - time.monotonic()

	@property
	def selected_host(self) -> str | None:
		"""Return the selected and locked host name."""
		return self._selected_host

	def get_placement_rate(self, host_name: str | None = None) -> float:
		"""Return VM creations per minute over five minutes, including drafts."""
		host = self._find_snapshot_host(host_name) if host_name is not None else None
		if host_name is not None:
			count = host.placement_count if host else 0
		else:
			count = self.usage.placement_count

		return count / (PLACEMENT_RATE_WINDOW.total_seconds() / 60)

	def _is_candidate(self, host_name: str) -> bool:
		"""Return whether this placement may choose the host. Without an allowed set, every host that is not excluded is a candidate."""
		if host_name in self._excluded_servers:
			return False

		if self._allowed_servers is None:
			return True

		return host_name in self._allowed_servers

	def try_select(self, host_name: str, *, wait: bool = False) -> bool:
		"""Hold a host lock only if its current capacity fits the request."""
		self.last_probe_was_contended = False
		if self._selected_host is not None:
			raise RuntimeError("A placement host is already selected.")
		if not self._is_candidate(host_name) or not self._find_snapshot_host(host_name):
			return False

		self.probe_count += 1
		lock_name = self._host_lock_name(host_name)
		if not self._acquire_host_lock(lock_name, wait=wait):
			self.has_contended_hosts = True
			self.last_probe_was_contended = True
			return False

		held_locks = [lock_name]
		try:
			is_selectable = self._check_locked_host(host_name, held_locks)
		except Exception:
			self._release_host_locks(held_locks)
			raise

		if not is_selectable:
			self._release_host_locks(held_locks)
			return False

		for held_lock in held_locks:
			self._keep_host_lock_for_transaction(held_lock)
		self._selected_host = host_name
		return True

	def _check_locked_host(self, host_name: str, held_locks: list[str]) -> bool:
		"""Check capacity and affinity while holding the host lock.

		A `within` rule reads the VMs of every host in the group of this host. Each of those
		hosts is locked too, so no VM lands there before this placement commits. Each extra
		lock is added to `held_locks`.
		"""
		if not self._host_has_capacity(host_name):
			return False

		for member in self._find_affinity_group(host_name):
			member_lock = self._host_lock_name(member)
			# Do not wait, so two placements that lock the same group cannot deadlock.
			if not self._acquire_host_lock(member_lock, wait=False):
				self.has_contended_hosts = True
				return False
			held_locks.append(member_lock)

		if not self._host_meets_affinity(host_name):
			# The snapshot missed a VM that a concurrent placement committed. Treat the
			# host as contended, so the next attempt reads a fresh snapshot.
			self.has_contended_hosts = True
			return False

		return True

	def _find_affinity_group(self, host_name: str) -> list[str]:
		"""Return the other hosts whose VMs the `within` rules read for this host."""
		if not self.apply_affinity or host_name == self.current_host_name:
			return []

		return self.requirements.placement_rules.find_related_hosts(host_name)

	def _release_host_locks(self, lock_names: list[str]) -> None:
		for lock_name in lock_names:
			self._release_host_lock(lock_name)

	@staticmethod
	def _host_lock_name(host_name: str) -> str:
		"""Return a server-global lock name scoped to this site database."""
		database_name = frappe.db.cur_db_name
		if not database_name:
			raise RuntimeError("Placement needs a database name to scope its host lock.")

		database_hash = sha256(database_name.encode()).hexdigest()[:12]
		host_hash = sha256(host_name.encode()).hexdigest()[:32]
		return f"atlas:placement:{database_hash}:{host_hash}"

	def _acquire_host_lock(self, lock_name: str, *, wait: bool) -> bool:
		"""Acquire the MariaDB lock for one host."""
		timeout = 0.0
		if wait:
			timeout = min(LOCK_WAIT_MAXIMUM_SECONDS, self.remaining_seconds)
			if timeout <= 0:
				return False

		rows = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, timeout))
		result = rows[0][0] if rows else None
		if result is None:
			raise RuntimeError(
				f"Placement could not acquire host lock {lock_name!r}; GET_LOCK returned NULL."
			)

		return result == 1

	@staticmethod
	def _release_host_lock(lock_name: str) -> None:
		rows = frappe.db.sql("SELECT RELEASE_LOCK(%s)", lock_name)
		result = rows[0][0] if rows else None
		if result != 1:
			raise RuntimeError(f"Placement lost host lock {lock_name!r}; RELEASE_LOCK returned {result!r}.")

	@classmethod
	def _release_host_lock_after_transaction(cls, lock_name: str) -> None:
		"""Report a lost lock without changing the completed transaction result."""
		try:
			cls._release_host_lock(lock_name)
		except Exception:
			frappe.logger("vm-placement").exception(
				"Failed to release placement lock %s after the transaction ended", lock_name
			)

	@classmethod
	def _keep_host_lock_for_transaction(cls, lock_name: str) -> None:
		def release() -> None:
			cls._release_host_lock_after_transaction(lock_name)

		frappe.db.after_commit.add(release)
		frappe.db.after_rollback.add(release)

	def _host_has_capacity(self, host_name: str) -> bool:
		"""Check committed memory and disk of one host while holding its placement lock."""
		memory_mib = self.requirements.memory_mib
		disk_mib = self.requirements.disk_mib
		if self.current_placement and self.current_placement.host_name == host_name:
			memory_mib -= self.current_placement.memory_mib
			disk_mib -= self.current_placement.disk_mib

		# CPU can be oversubscribed, so only memory and disk decide.
		rows = self._load_capacity_rows(host_name)
		return bool(rows) and rows[0].free_memory_mib >= memory_mib and rows[0].free_storage_mib >= disk_mib

	def _find_snapshot_host(self, host_name: str) -> HostUsage | None:
		return next((host for host in self.usage.hosts if host.name == host_name), None)

	def _load_fleet_usage(self, cache_snapshot: bool) -> FleetUsage:
		"""Build a fleet view from cached or current rows, without hosts that fail the affinity rules."""
		rows = self._load_cached_snapshot_rows() if cache_snapshot else self._load_snapshot_rows()
		return self._build_fleet_usage(self._filter_by_affinity(rows))

	def _load_cached_snapshot_rows(self) -> list[frappe._dict]:
		cache = frappe.cache()
		pool = int(self.requirements.is_sleepy) if self.use_dedicated_sleepy_vm_hosts else "any"
		key = "atlas:placement-snapshot:{0}:{1}:{2}".format(
			self.requirements.architecture, pool, self.requirements.tenant_id
		)
		rows = cache.get_value(key)
		if rows is None:
			rows = self._load_snapshot_rows()
			cache.set_value(key, rows, expires_in_sec=SNAPSHOT_CACHE_SECONDS)

		return rows

	def _filter_by_affinity(self, rows: list[frappe._dict]) -> list[frappe._dict]:
		"""Keep only the hosts that meet the affinity rules, when one of them has room.

		Without such a host, `Preferred` matching keeps every host. `Enforced` matching fails, but a
		resize keeps the matching hosts and its current host, so an in-place resize can still run.
		"""
		if not self.requirements.placement_rules.nodes:
			return rows

		allowed = set(self._find_affinity_hosts([row.name for row in rows]))
		matching = [row for row in rows if row.name in allowed or row.name == self.current_host_name]
		if any(self._has_room(row) for row in matching):
			self.apply_affinity = True
			return matching

		affinity_matching = load_affinity_matching()
		if affinity_matching == "Enforced":
			if self.current_host_name:
				self.apply_affinity = True
				return matching
			frappe.throw(
				_("No Metal Server that meets the affinity rules has capacity."), exc=AffinityUnsatisfied
			)
		elif affinity_matching == "Preferred":
			return rows
		else:
			assert_never(affinity_matching)

	def _has_room(self, row: frappe._dict) -> bool:
		"""Return whether a snapshot row reports room for the request, as a strategy checks it."""
		return (
			self._is_candidate(row.name)
			and row.free_memory_mib >= self.requirements.memory_mib
			and row.free_storage_mib >= self.requirements.disk_mib
		)

	def _host_meets_affinity(self, host_name: str) -> bool:
		"""Check the affinity rules again under the host lock, against committed VMs.

		Concurrent placements can share one snapshot. The host lock orders them, so the
		second placement reads the committed draft of the first one here.
		"""
		if not self.apply_affinity or host_name == self.current_host_name:
			return True

		return bool(self._find_affinity_hosts([host_name]))

	def _find_affinity_hosts(self, host_names: list[str]) -> list[str]:
		requirements = self.requirements
		return requirements.placement_rules.filter_hosts(
			host_names, requirements.tenant_id, requirements.virtual_machine
		)

	def _load_snapshot_rows(self) -> list[frappe._dict]:
		"""Load the capacity rows of the host pool with the counts that strategies rank by."""
		rows = self._load_capacity_rows()
		if not rows:
			return rows

		host_names = [row.name for row in rows]
		tenant_vm_counts = self._count_tenant_virtual_machines(host_names)
		placement_counts = Counter(
			frappe.get_all(
				"Virtual Machine",
				filters={
					"server": ("in", host_names),
					"creation": (">=", self._created_at - PLACEMENT_RATE_WINDOW),
				},
				pluck="server",
			)
		)
		sleepy_memory = self._sum_sleepy_memory(host_names)
		for row in rows:
			row.tenant_vm_count = tenant_vm_counts[row.name]
			row.placement_count = placement_counts[row.name]
			row.sleepy_reserved_memory_mib = sleepy_memory[row.name]
		return rows

	def _load_capacity_rows(self, host_name: str | None = None) -> list[frappe._dict]:
		"""Return each eligible host with a fresh sample and the capacity it has left, by host name.

		A sample can be 2 minutes old. Free capacity subtracts what it cannot include yet: drafts,
		VMs created after it, and the shapes of migrations into the host.
		"""
		hosts = self._load_eligible_hosts(host_name)
		samples = self._load_latest_samples(list(hosts))
		reservations = self._load_reservations(samples)

		rows = []
		for name in sorted(samples):
			sample = samples[name]
			reserved = self._sum_resources(reservations[name])
			free = Resources(
				sample.available_cpu_millicores, sample.available_memory_mib, sample.available_storage_mib
			).reserve(reserved.cpu_millicores, reserved.memory_mib, reserved.storage_mib)
			rows.append(
				frappe._dict(
					name=name,
					architecture=hosts[name].architecture,
					is_sleepy_vm_host=hosts[name].is_sleepy_vm_host,
					sample_created_at=sample.creation,
					total_cpu_millicores=sample.total_cpu_millicores,
					total_memory_mib=sample.total_memory_mib,
					total_storage_mib=sample.total_storage_mib,
					free_cpu_millicores=free.cpu_millicores,
					free_memory_mib=free.memory_mib,
					free_storage_mib=free.storage_mib,
				)
			)
		return rows

	def _load_eligible_hosts(self, host_name: str | None = None) -> dict[str, frappe._dict]:
		"""Return the running, set-up hosts of the required architecture and pool, by name."""
		filters = {
			"status": "Running",
			"is_provisioning_completed": 1,
			"is_in_maintenance": 0,
			"architecture": self.requirements.architecture,
		}
		if self.use_dedicated_sleepy_vm_hosts:
			filters["is_sleepy_vm_host"] = int(self.requirements.is_sleepy)
		if host_name:
			filters["name"] = host_name

		hosts = frappe.get_all(
			"Metal Server", filters=filters, fields=["name", "architecture", "is_sleepy_vm_host"]
		)
		return {host.name: host for host in hosts}

	def _load_latest_samples(self, host_names: list[str]) -> dict[str, frappe._dict]:
		"""Return the newest capacity sample of each host, when it is at most 2 minutes old."""
		if not host_names:
			return {}

		samples = frappe.get_all(
			"Metal Server Usage",
			filters={
				"server": ("in", host_names),
				"creation": (">=", self._created_at - CAPACITY_MAXIMUM_AGE),
			},
			fields=[
				"server",
				"creation",
				"total_cpu_millicores",
				"total_memory_mib",
				"total_storage_mib",
				"available_cpu_millicores",
				"available_memory_mib",
				"available_storage_mib",
			],
			order_by="creation desc, name desc",
		)
		latest: dict[str, frappe._dict] = {}
		for sample in samples:
			latest.setdefault(sample.server, sample)
		return latest

	def _load_reservations(self, samples: dict[str, frappe._dict]) -> defaultdict[str, list[Resources]]:
		"""Return what the sample of each host cannot include yet, as one shape per VM."""
		reservations: defaultdict[str, list[Resources]] = defaultdict(list)
		if not samples:
			return reservations

		new_virtual_machines = frappe.get_all(
			"Virtual Machine",
			filters={
				"server": ("in", list(samples)),
				"creation": (">=", self._created_at - RESERVATION_MAXIMUM_AGE),
			},
			fields=["server", "creation", "is_draft", "cpu_millicores", "memory_mib", "disk_mib"],
		)
		for virtual_machine in new_virtual_machines:
			if (
				virtual_machine.is_draft
				or virtual_machine.creation > samples[virtual_machine.server].creation
			):
				reservations[virtual_machine.server].append(
					Resources(
						virtual_machine.cpu_millicores, virtual_machine.memory_mib, virtual_machine.disk_mib
					)
				)

		for migration in self._load_incoming_migrations(list(samples)):
			reservations[migration.server].append(self._get_migration_shape(migration))
		return reservations

	@staticmethod
	def _load_incoming_migrations(host_names: list[str]) -> list[frappe._dict]:
		"""Return each migration that reserves capacity on one of the hosts, with its VM shape."""
		migration = frappe.qb.DocType("Virtual Machine Migration")
		virtual_machine = frappe.qb.DocType("Virtual Machine")
		query = (
			frappe.qb.from_(migration)
			.join(virtual_machine)
			.on(virtual_machine.name == migration.virtual_machine)
			.select(
				migration.destination_metal_server.as_("server"),
				migration.target_cpu_millicores,
				migration.target_memory_mib,
				migration.target_disk_mib,
				virtual_machine.tenant_id,
				virtual_machine.cpu_millicores,
				virtual_machine.memory_mib,
				virtual_machine.disk_mib,
			)
			.where(
				migration.destination_metal_server.isin(host_names)
				& migration.status.isin(RESERVED_MIGRATION_STATUSES)
			)
		)
		return query.run(as_dict=True)

	@staticmethod
	def _get_migration_shape(migration: frappe._dict) -> Resources:
		"""Return the target shape of a migration that resizes the VM, else the current VM shape."""
		if migration.target_memory_mib > 0:
			return Resources(
				migration.target_cpu_millicores, migration.target_memory_mib, migration.target_disk_mib
			)
		return Resources(migration.cpu_millicores, migration.memory_mib, migration.disk_mib)

	def _count_tenant_virtual_machines(self, host_names: list[str]) -> Counter[str]:
		"""Count the VMs of the tenant on each host, and again on the destination of an active migration."""
		tenant_id = self.requirements.tenant_id
		counts = Counter(
			frappe.get_all(
				"Virtual Machine",
				filters={"server": ("in", host_names), "tenant_id": tenant_id},
				pluck="server",
			)
		)
		counts.update(
			migration.server
			for migration in self._load_incoming_migrations(host_names)
			if migration.tenant_id == tenant_id
		)
		return counts

	def _sum_sleepy_memory(self, host_names: list[str]) -> Counter[str]:
		"""Sum the memory of sleepy VMs on each host, which only a sleepy placement ranks by."""
		memory: Counter[str] = Counter()
		if not self.requirements.is_sleepy:
			return memory

		for virtual_machine in frappe.get_all(
			"Virtual Machine",
			filters={"server": ("in", host_names), "sleep_after_idle_seconds": (">", 0)},
			fields=["server", "memory_mib"],
		):
			memory[virtual_machine.server] += virtual_machine.memory_mib
		return memory

	def _build_fleet_usage(self, rows: list[frappe._dict]) -> FleetUsage:
		"""Build the immutable view a strategy reads, dropping excluded hosts."""
		hosts = tuple(
			HostUsage(
				name=row.name,
				architecture=row.architecture,
				is_sleepy=bool(row.is_sleepy_vm_host),
				sample_created_at=row.sample_created_at,
				total=Resources(
					row.total_cpu_millicores,
					row.total_memory_mib,
					row.total_storage_mib,
				),
				free=Resources(
					row.free_cpu_millicores,
					row.free_memory_mib,
					row.free_storage_mib,
				),
				tenant_vm_count=row.tenant_vm_count,
				sleepy_reserved_memory_mib=row.sleepy_reserved_memory_mib,
				placement_count=row.placement_count,
			)
			for row in rows
			if self._is_candidate(row.name)
		)
		return FleetUsage(
			hosts=hosts,
			total=self._sum_resources(host.total for host in hosts),
			free=self._sum_resources(host.free for host in hosts),
			tenant_vm_count=sum(host.tenant_vm_count for host in hosts),
			sleepy_reserved_memory_mib=sum(host.sleepy_reserved_memory_mib for host in hosts),
			placement_count=sum(host.placement_count for host in hosts),
		)

	@staticmethod
	def _sum_resources(resources: Iterable[Resources]) -> Resources:
		items = tuple(resources)
		return Resources(
			cpu_millicores=sum(item.cpu_millicores for item in items),
			memory_mib=sum(item.memory_mib for item in items),
			storage_mib=sum(item.storage_mib for item in items),
		)
