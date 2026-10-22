"""Keep VMs stable and balance new placements across suitable hosts."""

from __future__ import annotations

from typing import override

from atlas.vm.core.placement.models import HostUsage
from atlas.vm.core.placement.strategies.base import PlacementStrategy, register

# Hosts in one memory band rank by storage, then CPU.
MEMORY_PRESSURE_BAND = 0.05


@register("balanced")
class BalancedStrategy(PlacementStrategy):
	"""Balance memory, storage, and CPU pressure.

	The strategy keeps a lifecycle operation on its current host when capacity permits.
	For a new placement, it ranks eligible hosts by these keys:

	1. The projected memory pressure in 5% bands, from low to high.
	2. The projected storage pressure, from low to high.
	3. The projected CPU pressure, from low to high.
	4. The host name.

	For a sleepy VM, the memory rank applies the configured overcommit factor. The
	locked capacity check does not apply this factor.
	"""

	@override
	def select_host(self) -> None:
		"""Choose a stable host while balancing fleet pressure."""
		host_pool = self.host_pool
		if self.try_current_host(host_pool):
			return

		eligible_hosts = self.get_eligible_hosts(host_pool)
		self.select_from_ranked(sorted(eligible_hosts, key=self._host_priority))

	def _host_priority(self, host: HostUsage) -> tuple[int, float, float, str]:
		"""Prefer a lower memory band, then lower storage pressure, then lower CPU pressure."""
		requirements = self.requirements
		factor = self.sleepy_vm_overcommit_factor if requirements.is_sleepy else 1.0
		used_memory_mib = host.total.memory_mib - host.free.memory_mib
		sleepy_memory_mib = min(host.sleepy_reserved_memory_mib, used_memory_mib)
		memory_pressure = (
			used_memory_mib - sleepy_memory_mib * (1 - 1 / factor) + requirements.memory_mib / factor
		) / host.total.memory_mib
		storage_pressure = (
			host.total.storage_mib - host.free.storage_mib + requirements.disk_mib
		) / host.total.storage_mib
		cpu_pressure = (
			host.total.cpu_millicores - host.free.cpu_millicores + requirements.cpu_millicores
		) / host.total.cpu_millicores

		return int(memory_pressure // MEMORY_PRESSURE_BAND), storage_pressure, cpu_pressure, host.name
