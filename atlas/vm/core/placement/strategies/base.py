"""Select a Metal Server for a set of placement requirements."""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import ClassVar, Never, TypeVar

import frappe
from frappe import _

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.vm.core.placement.capacity_expansion import enqueue_capacity_expansion
from atlas.vm.core.placement.context import (
	PlacementContext,
	is_pool_known_full,
	remember_pool_is_full,
)
from atlas.vm.core.placement.models import (
	CurrentPlacement,
	FleetUsage,
	HostUsage,
	PlacementRequirements,
)

Strategy = TypeVar("Strategy", bound="PlacementStrategy")
_REGISTERED_STRATEGIES: dict[str, type[PlacementStrategy]] = {}

PLACEMENT_ATTEMPTS = 3
PLACEMENT_BACKOFF_SECONDS = 0.02
# One deadline covers retries and lock waits.
PLACEMENT_DEADLINE_SECONDS = 0.4
# Limit stale candidates that reach the database.
PLACEMENT_PROBE_LIMIT = 16


class OutOfCapacity(AtlasUserError):
	"""No ready Metal Server can hold the requested VM."""

	code = "out_of_capacity"
	http_status_code = 503


class PlacementBusy(AtlasUserError):
	"""Every candidate host was locked by another placement."""

	code = "placement_busy"
	http_status_code = 503
	# Contending placements finish within one deadline.
	headers: ClassVar[dict[str, str]] = {"Retry-After": "1"}


def register(name: str) -> Callable[[type[Strategy]], type[Strategy]]:
	"""Register a placement strategy under its settings value."""

	def decorator(strategy: type[Strategy]) -> type[Strategy]:
		_REGISTERED_STRATEGIES[name] = strategy
		return strategy

	return decorator


class PlacementStrategy(ABC):
	"""Rank ready hosts and select one that has enough capacity.

	A strategy selects a host pool, removes hosts without enough capacity, and ranks the
	remaining hosts. The placement session locks each candidate and checks its
	current capacity. Thus, a strategy controls preference but not admission.
	"""

	def __init__(self, placement: PlacementContext) -> None:
		self._placement = placement

	@property
	def requirements(self) -> PlacementRequirements:
		"""Return the resources and constraints for this placement."""
		return self._placement.requirements

	@property
	def action(self) -> str:
		"""Return the operation that needs placement."""
		return self._placement.action

	@property
	def current_host_name(self) -> str | None:
		"""Return the current host for a non-create operation."""
		return self._placement.current_host_name

	@property
	def usage(self) -> FleetUsage:
		"""Return the immutable fleet usage snapshot."""
		return self._placement.usage

	@property
	def sleepy_vm_overcommit_factor(self) -> float:
		"""Return the configured sleepy VM memory factor."""
		return self._placement.sleepy_vm_overcommit_factor

	@property
	def host_pool(self) -> tuple[HostUsage, ...]:
		"""Return ready hosts that match the required architecture and pool."""
		return tuple(
			host
			for host in self.usage.hosts
			if host.architecture == self.requirements.architecture
			and (
				not self._placement.use_dedicated_sleepy_vm_hosts
				or host.is_sleepy == self.requirements.is_sleepy
			)
		)

	@abstractmethod
	def select_host(self) -> None:
		"""Select one host, or leave the placement without a selected host.

		Use this sequence:

		1. Read `host_pool`.
		2. Call `try_current_host` for a lifecycle operation.
		3. Call `get_eligible_hosts` to remove hosts without reported capacity.
		4. Rank the eligible hosts.
		5. Pass the ranked hosts to `select_from_ranked`.

		Example:

		    @register("example")
		    class ExampleStrategy(PlacementStrategy):
		        @override
		        def select_host(self) -> None:
		            host_pool = self.host_pool
		            if self.try_current_host(host_pool):
		                return

		            eligible_hosts = self.get_eligible_hosts(host_pool)
		            self.select_from_ranked(eligible_hosts)
		"""

	def try_current_host(self, pool: Sequence[HostUsage]) -> bool:
		"""Keep a non-create operation on its current host, waiting for its lock."""
		if self.action == "create" or not self.current_host_name:
			return False
		if not any(host.name == self.current_host_name for host in pool):
			return False
		return self.try_select(self.current_host_name, wait=True)

	def get_eligible_hosts(self, pool: Sequence[HostUsage]) -> list[HostUsage]:
		"""Return hosts with enough reported memory and storage."""
		return [
			host
			for host in pool
			if host.free.memory_mib >= self.requirements.memory_mib
			and host.free.storage_mib >= self.requirements.disk_mib
		]

	def get_placement_rate(self, host_name: str | None = None) -> float:
		"""Return the recent placement rate for a host or the fleet."""
		return self._placement.get_placement_rate(host_name)

	def try_select(self, host_name: str, *, wait: bool = False) -> bool:
		"""Lock and recheck one host, and report whether this placement selected it."""
		return self._placement.try_select(host_name, wait=wait)

	def select_from_ranked(self, hosts: Sequence[HostUsage]) -> None:
		"""Select the first host in rank order that is free and still fits.

		Rank order gives the preferred host. Concurrent placements rank hosts the
		same way, so after one host turns out to be busy the rest are tried in
		random order. Select nothing when no candidate is free before the deadline.
		"""
		remaining = list(hosts)
		contended = []
		probe_count = 0
		while remaining and probe_count < PLACEMENT_PROBE_LIMIT:
			if self._placement.remaining_seconds <= 0:
				return

			host = remaining.pop(0)
			probe_count += 1
			if self.try_select(host.name):
				return
			if self._placement.last_probe_was_contended:
				contended.append(host)
				random.shuffle(remaining)

		if contended:
			self.try_select(random.choice(contended).name, wait=True)

	@staticmethod
	def get_provisioning_ratio(host: HostUsage, requirements: PlacementRequirements | None = None) -> float:
		"""Return the larger memory or storage fraction after an optional placement."""
		additional_memory = requirements.memory_mib if requirements else 0
		additional_storage = requirements.disk_mib if requirements else 0
		return max(
			(host.total.memory_mib - host.free.memory_mib + additional_memory) / host.total.memory_mib,
			(host.total.storage_mib - host.free.storage_mib + additional_storage) / host.total.storage_mib,
		)

	@classmethod
	def find_server(
		cls,
		requirements: PlacementRequirements,
		*,
		exclude_servers: set[str] | None = None,
		current_placement: CurrentPlacement | None = None,
	) -> str:
		"""Return a locked host name or report that the pool has no capacity.

		A resize passes its current placement, so the strategy keeps the VM on its
		host when the host has room for the increase.
		"""
		# A full pool can still hold the increase on the current host.
		if not current_placement and is_pool_known_full(requirements):
			cls._report_out_of_capacity(requirements)

		strategy_name, overcommit_factor, dedicated_sleepy_hosts = cls._load_settings()
		deadline = time.monotonic() + PLACEMENT_DEADLINE_SECONDS
		is_pool_full = False
		is_contended = False
		for attempt in range(PLACEMENT_ATTEMPTS):
			placement = PlacementContext(
				requirements,
				overcommit_factor,
				exclude_servers,
				cache_snapshot=attempt == 0,
				deadline=deadline,
				use_dedicated_sleepy_vm_hosts=dedicated_sleepy_hosts,
				current_placement=current_placement,
			)
			cls.bind_strategy(strategy_name, placement).select_host()
			if placement.selected_host is not None:
				return placement.selected_host

			if not placement.has_contended_hosts:
				# Only an unprobed snapshot with time remaining can prove the pool is full.
				is_pool_full = placement.probe_count == 0 and placement.remaining_seconds > 0
				break

			is_contended = True
			remaining = deadline - time.monotonic()
			if remaining <= 0 or attempt == PLACEMENT_ATTEMPTS - 1:
				break

			cls._wait_before_retry(attempt, remaining)

		if is_pool_full:
			remember_pool_is_full(requirements)

		# Contention must not trigger capacity expansion.
		if is_contended:
			cls._report_placement_busy()

		cls._report_out_of_capacity(requirements)

	@classmethod
	def reserve_server(
		cls,
		requirements: PlacementRequirements,
		server_name: str,
		*,
		exclude_servers: set[str] | None = None,
	) -> str:
		"""Reserve one named host when it still satisfies the requirements."""
		_, overcommit_factor, dedicated_sleepy_hosts = cls._load_settings()
		deadline = time.monotonic() + PLACEMENT_DEADLINE_SECONDS
		for attempt in range(PLACEMENT_ATTEMPTS):
			placement = PlacementContext(
				requirements,
				overcommit_factor,
				exclude_servers,
				deadline=deadline,
				use_dedicated_sleepy_vm_hosts=dedicated_sleepy_hosts,
			)
			if placement.try_select(server_name, wait=True):
				return server_name
			if not placement.has_contended_hosts:
				break

			remaining = deadline - time.monotonic()
			if remaining <= 0 or attempt == PLACEMENT_ATTEMPTS - 1:
				break

			cls._wait_before_retry(attempt, remaining)

		if placement.has_contended_hosts:
			cls._report_placement_busy()

		frappe.throw(
			_("Metal Server {0} is not ready or has no current capacity for this Virtual Machine.").format(
				server_name
			),
			exc=OutOfCapacity,
		)

	@classmethod
	def bind_strategy(cls, name: str, placement: PlacementContext) -> PlacementStrategy:
		"""Bind a registered strategy to one placement session."""
		return cls.get_strategy_class(name)(placement)

	@classmethod
	def get_strategy_class(cls, name: str) -> type[PlacementStrategy]:
		"""Return the strategy class registered under a settings value."""
		strategy = _REGISTERED_STRATEGIES.get(name)
		if strategy is None:
			frappe.throw(_("Unknown placement strategy: {0}.").format(name))
		return strategy

	@classmethod
	def registered_names(cls) -> tuple[str, ...]:
		"""Return the configured placement strategy values."""
		return tuple(_REGISTERED_STRATEGIES)

	def get_sleepy_host_priority(self, host: HostUsage) -> tuple[float, float, str]:
		"""Pack subscribed sleepy memory, then provisioned memory and storage."""
		requirements = self.requirements
		return (
			-(host.sleepy_reserved_memory_mib + requirements.memory_mib) / host.total.memory_mib,
			-self.get_provisioning_ratio(host, requirements),
			host.name,
		)

	@classmethod
	def _report_out_of_capacity(cls, requirements: PlacementRequirements) -> Never:
		"""Queue capacity expansion, then tell the caller to retry later."""
		enqueue_capacity_expansion(requirements)
		frappe.throw(_("No Metal Server has capacity. Retry later."), exc=OutOfCapacity)

	@staticmethod
	def _report_placement_busy() -> Never:
		"""Report contention. The fleet has room, so it is not expanded."""
		frappe.throw(
			_("Every candidate Metal Server is busy with another placement. Retry now."),
			exc=PlacementBusy,
		)

	@staticmethod
	def _load_settings() -> tuple[str, float, bool]:
		"""Read the settings placement needs, not the whole document."""
		strategy_name, overcommit_factor, dedicated_sleepy_hosts = frappe.get_cached_value(
			"Atlas Settings",
			"Atlas Settings",
			["placement_strategy", "sleepy_vm_overcommit_factor", "use_dedicated_sleepy_vm_hosts"],
		)
		return strategy_name, overcommit_factor, bool(dedicated_sleepy_hosts)

	@staticmethod
	def _wait_before_retry(attempt: int, remaining: float) -> None:
		"""Spread contending placements without spending more than the time that is left."""
		time.sleep(min(random.uniform(0, PLACEMENT_BACKOFF_SECONDS * 2**attempt), remaining))
