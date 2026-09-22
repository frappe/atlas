from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import timedelta
from hashlib import sha256

import frappe
from frappe.utils import now_datetime

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
		self.has_contended_hosts = False
		self.probe_count = 0
		self.last_probe_was_contended = False
		self._deadline = deadline
		self._excluded_servers = frozenset(exclude_servers or ())
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

	def try_select(self, host_name: str, *, wait: bool = False) -> bool:
		"""Hold a host lock only if its current capacity fits the request."""
		self.last_probe_was_contended = False
		if self._selected_host is not None:
			raise RuntimeError("A placement host is already selected.")
		if host_name in self._excluded_servers or not self._find_snapshot_host(host_name):
			return False

		self.probe_count += 1
		lock_name = self._host_lock_name(host_name)
		if not self._acquire_host_lock(lock_name, wait=wait):
			self.has_contended_hosts = True
			self.last_probe_was_contended = True
			return False

		try:
			has_capacity = self._host_has_capacity(host_name)
		except Exception:
			self._release_host_lock(lock_name)
			raise

		if not has_capacity:
			self._release_host_lock(lock_name)
			return False

		self._keep_host_lock_for_transaction(lock_name)
		self._selected_host = host_name
		return True

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
		"""Check host capacity while holding its placement lock."""
		memory_mib = self.requirements.memory_mib
		disk_mib = self.requirements.disk_mib
		if self.current_placement and self.current_placement.host_name == host_name:
			memory_mib -= self.current_placement.memory_mib
			disk_mib -= self.current_placement.disk_mib
		sleepy_host_filter = self._sleepy_host_filter()
		# The interpolated filter is a literal fragment. No caller value reaches the query.
		rows = frappe.db.sql(  # nosemgrep
			f"""
				SELECT server.name
				FROM `tabMetal Server` AS server
				WHERE server.name = %(host_name)s
					AND server.status = 'Running'
					AND server.is_provisioning_completed = 1
					AND server.architecture = %(architecture)s
					{sleepy_host_filter}
					AND COALESCE((
						SELECT
							GREATEST(
								sample.available_memory_mib
									- COALESCE((
										SELECT SUM(vm.memory_mib)
										FROM `tabVirtual Machine` AS vm
										WHERE vm.server = server.name
											AND vm.creation >= %(reservation_cutoff)s
											AND (vm.is_draft = 1 OR vm.creation > sample.creation)
									), 0)
									- COALESCE((
										SELECT SUM(IF(migration.target_memory_mib > 0, migration.target_memory_mib, vm.memory_mib))
										FROM `tabVirtual Machine Migration` AS migration
										INNER JOIN `tabVirtual Machine` AS vm
											ON vm.name = migration.virtual_machine
										WHERE migration.destination_metal_server = server.name
											AND migration.status IN ('preparing', 'copying', 'cutting_over', 'starting', 'finalizing', 'canceling')
									), 0),
								0
							) >= %(memory_mib)s
							AND GREATEST(
								sample.available_storage_mib
									- COALESCE((
										SELECT SUM(vm.disk_mib)
										FROM `tabVirtual Machine` AS vm
										WHERE vm.server = server.name
											AND vm.creation >= %(reservation_cutoff)s
											AND (vm.is_draft = 1 OR vm.creation > sample.creation)
									), 0)
									- COALESCE((
										SELECT SUM(IF(migration.target_memory_mib > 0, migration.target_disk_mib, vm.disk_mib))
										FROM `tabVirtual Machine Migration` AS migration
										INNER JOIN `tabVirtual Machine` AS vm
											ON vm.name = migration.virtual_machine
										WHERE migration.destination_metal_server = server.name
											AND migration.status IN ('preparing', 'copying', 'cutting_over', 'starting', 'finalizing', 'canceling')
									), 0),
								0
							) >= %(disk_mib)s
						FROM `tabMetal Server Usage` AS sample
						WHERE sample.server = server.name
							AND sample.creation >= %(capacity_cutoff)s
						ORDER BY sample.creation DESC, sample.name DESC
						LIMIT 1
					), 0) = 1
				""",
			{
				"host_name": host_name,
				"architecture": self.requirements.architecture,
				"is_sleepy": int(self.requirements.is_sleepy),
				"memory_mib": memory_mib,
				"disk_mib": disk_mib,
				"capacity_cutoff": self._created_at - CAPACITY_MAXIMUM_AGE,
				"reservation_cutoff": self._created_at - RESERVATION_MAXIMUM_AGE,
			},
		)
		return bool(rows)

	def _sleepy_host_filter(self) -> str:
		"""Keep the sleepy pool apart only while the site asks for dedicated hosts."""
		if not self.use_dedicated_sleepy_vm_hosts:
			return ""

		return "AND server.is_sleepy_vm_host = %(is_sleepy)s"

	def _find_snapshot_host(self, host_name: str) -> HostUsage | None:
		return next((host for host in self.usage.hosts if host.name == host_name), None)

	def _load_fleet_usage(self, cache_snapshot: bool) -> FleetUsage:
		"""Build a fleet view from cached or current rows."""
		if not cache_snapshot:
			return self._build_fleet_usage(self._load_snapshot_rows())

		cache = frappe.cache()
		pool = int(self.requirements.is_sleepy) if self.use_dedicated_sleepy_vm_hosts else "any"
		key = "atlas:placement-snapshot:{0}:{1}:{2}".format(
			self.requirements.architecture, pool, self.requirements.tenant_id
		)
		rows = cache.get_value(key)
		if rows is None:
			rows = self._load_snapshot_rows()
			cache.set_value(key, rows, expires_in_sec=SNAPSHOT_CACHE_SECONDS)

		return self._build_fleet_usage(rows)

	def _load_snapshot_rows(self) -> list[frappe._dict]:
		"""Load current capacity and rank data for the required host pool."""
		sleepy_host_filter = self._sleepy_host_filter()
		# The interpolated filter is a literal fragment. No caller value reaches the query.
		rows = frappe.db.sql(  # nosemgrep
			f"""
			WITH eligible_servers AS (
				SELECT server.name, server.architecture, server.is_sleepy_vm_host
				FROM `tabMetal Server` AS server
				WHERE server.status = 'Running'
					AND server.is_provisioning_completed = 1
					AND server.architecture = %(architecture)s
					{sleepy_host_filter}
			),
			ranked_sample AS (
				SELECT
					sample.*,
					ROW_NUMBER() OVER (
						PARTITION BY sample.server
						ORDER BY sample.creation DESC, sample.name DESC
					) AS sample_rank
				FROM `tabMetal Server Usage` AS sample
				INNER JOIN eligible_servers AS server ON server.name = sample.server
				WHERE sample.creation >= %(capacity_cutoff)s
			),
			latest_sample AS (
				SELECT * FROM ranked_sample WHERE sample_rank = 1
			),
			reserved_usage AS (
				SELECT
					vm.server,
					SUM(vm.cpu_millicores) AS cpu_millicores,
					SUM(vm.memory_mib) AS memory_mib,
					SUM(vm.disk_mib) AS storage_mib
				FROM `tabVirtual Machine` AS vm
				INNER JOIN latest_sample AS sample ON sample.server = vm.server
				WHERE vm.creation >= %(reservation_cutoff)s
					AND (vm.is_draft = 1 OR vm.creation > sample.creation)
				GROUP BY vm.server
			),
			tenant_usage AS (
				SELECT vm.server, COUNT(*) AS tenant_vm_count
				FROM `tabVirtual Machine` AS vm
				INNER JOIN eligible_servers AS server ON server.name = vm.server
				WHERE vm.tenant_id = %(tenant_id)s
				GROUP BY vm.server
			),
			sleepy_usage AS (
				SELECT vm.server, SUM(vm.memory_mib) AS sleepy_reserved_memory_mib
				FROM `tabVirtual Machine` AS vm
				INNER JOIN eligible_servers AS server ON server.name = vm.server
				WHERE %(is_sleepy)s = 1 AND vm.sleep_after_idle_seconds > 0
				GROUP BY vm.server
			),
			rate_usage AS (
				SELECT vm.server, COUNT(*) AS placement_count
				FROM `tabVirtual Machine` AS vm
				INNER JOIN eligible_servers AS server ON server.name = vm.server
				WHERE vm.creation >= %(rate_cutoff)s
				GROUP BY vm.server
			),
			migration_usage AS (
				SELECT
					migration.destination_metal_server AS server,
					SUM(IF(migration.target_memory_mib > 0, migration.target_cpu_millicores, vm.cpu_millicores))
						AS cpu_millicores,
					SUM(IF(migration.target_memory_mib > 0, migration.target_memory_mib, vm.memory_mib))
						AS memory_mib,
					SUM(IF(migration.target_memory_mib > 0, migration.target_disk_mib, vm.disk_mib))
						AS storage_mib
				FROM `tabVirtual Machine Migration` AS migration
				INNER JOIN `tabVirtual Machine` AS vm ON vm.name = migration.virtual_machine
				WHERE migration.status IN ('preparing', 'copying', 'cutting_over', 'starting', 'finalizing', 'canceling')
				GROUP BY migration.destination_metal_server
			)
			SELECT
				server.name,
				server.architecture,
				server.is_sleepy_vm_host,
				sample.creation AS sample_created_at,
				sample.total_cpu_millicores,
				sample.total_memory_mib,
				sample.total_storage_mib,
				GREATEST(
					sample.available_cpu_millicores
						- COALESCE(reserved_usage.cpu_millicores, 0)
						- COALESCE(migration_usage.cpu_millicores, 0),
					0
				) AS free_cpu_millicores,
				GREATEST(
					sample.available_memory_mib
						- COALESCE(reserved_usage.memory_mib, 0)
						- COALESCE(migration_usage.memory_mib, 0),
					0
				) AS free_memory_mib,
				GREATEST(
					sample.available_storage_mib
						- COALESCE(reserved_usage.storage_mib, 0)
						- COALESCE(migration_usage.storage_mib, 0),
					0
				) AS free_storage_mib,
				COALESCE(tenant_usage.tenant_vm_count, 0) AS tenant_vm_count,
				COALESCE(sleepy_usage.sleepy_reserved_memory_mib, 0) AS sleepy_reserved_memory_mib,
				COALESCE(rate_usage.placement_count, 0) AS placement_count
			FROM eligible_servers AS server
			INNER JOIN latest_sample AS sample ON sample.server = server.name
			LEFT JOIN reserved_usage ON reserved_usage.server = server.name
			LEFT JOIN tenant_usage ON tenant_usage.server = server.name
			LEFT JOIN sleepy_usage ON sleepy_usage.server = server.name
			LEFT JOIN rate_usage ON rate_usage.server = server.name
			LEFT JOIN migration_usage ON migration_usage.server = server.name
			ORDER BY server.name
			""",
			{
				"architecture": self.requirements.architecture,
				"is_sleepy": int(self.requirements.is_sleepy),
				"tenant_id": self.requirements.tenant_id,
				"capacity_cutoff": self._created_at - CAPACITY_MAXIMUM_AGE,
				"reservation_cutoff": self._created_at - RESERVATION_MAXIMUM_AGE,
				"rate_cutoff": self._created_at - PLACEMENT_RATE_WINDOW,
			},
			as_dict=True,
		)
		return rows

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
			if row.name not in self._excluded_servers
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
