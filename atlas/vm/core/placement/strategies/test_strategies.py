from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch

from atlas.vm.core.placement.models import (
	CurrentPlacement,
	FleetUsage,
	HostUsage,
	PlacementRequirements,
	Resources,
)
from atlas.vm.core.placement.strategies.balanced import BalancedStrategy
from atlas.vm.core.placement.strategies.base import (
	PLACEMENT_ATTEMPTS,
	PLACEMENT_DEADLINE_SECONDS,
	PLACEMENT_PROBE_LIMIT,
	OutOfCapacity,
	PlacementBusy,
	PlacementStrategy,
)
from atlas.vm.core.placement.strategies.best_fit import BestFitStrategy
from atlas.vm.core.placement.strategies.spread_3 import SpreadThreeStrategy


class TestComparisonStrategies(TestCase):
	@staticmethod
	def _host(
		name: str,
		*,
		architecture: str = "amd64",
		is_sleepy: bool = False,
		free_cpu_millicores: int = 10000,
		used_memory_mib: int = 0,
		used_storage_mib: int = 0,
		tenant_vm_count: int = 0,
		subscribed_memory_mib: int = 0,
	) -> HostUsage:
		return HostUsage(
			name=name,
			architecture=architecture,
			is_sleepy=is_sleepy,
			sample_created_at=datetime(2026, 9, 18),
			total=Resources(10000, 10000, 10000),
			free=Resources(free_cpu_millicores, 10000 - used_memory_mib, 10000 - used_storage_mib),
			tenant_vm_count=tenant_vm_count,
			sleepy_reserved_memory_mib=subscribed_memory_mib,
			placement_count=0,
		)

	@staticmethod
	def _placement(
		hosts: tuple[HostUsage, ...],
		*,
		is_sleepy: bool = False,
		factor: float = 1.0,
		rates: dict[str, float] | None = None,
		memory_mib: int = 1000,
		storage_mib: int = 1000,
		action: str = "create",
		current_host_name: str | None = None,
	) -> SimpleNamespace:
		total = Resources(
			sum(host.total.cpu_millicores for host in hosts),
			sum(host.total.memory_mib for host in hosts),
			sum(host.total.storage_mib for host in hosts),
		)
		free = Resources(
			sum(host.free.cpu_millicores for host in hosts),
			sum(host.free.memory_mib for host in hosts),
			sum(host.free.storage_mib for host in hosts),
		)
		return SimpleNamespace(
			requirements=PlacementRequirements(1000, memory_mib, storage_mib, "amd64", 7, is_sleepy),
			usage=FleetUsage(hosts, total, free, 0, 0, 0),
			action=action,
			current_host_name=current_host_name,
			sleepy_vm_overcommit_factor=factor,
			get_placement_rate=Mock(side_effect=lambda host_name: (rates or {}).get(host_name, 0.0)),
			try_select=Mock(return_value=True),
			has_contended_hosts=False,
			last_probe_was_contended=False,
			remaining_seconds=PLACEMENT_DEADLINE_SECONDS,
			use_dedicated_sleepy_vm_hosts=True,
		)

	def test_spread_selects_the_least_provisioned_host(self) -> None:
		placement = self._placement(
			(
				self._host("fuller", used_memory_mib=8400),
				self._host("middle", used_memory_mib=8300),
				self._host("least", used_memory_mib=8200),
			)
		)

		SpreadThreeStrategy(placement).select_host()

		placement.try_select.assert_called_once_with("least", wait=False)

	def test_spread_keeps_a_regular_request_off_a_sleepy_host(self) -> None:
		placement = self._placement((self._host("regular"), self._host("sleepy", is_sleepy=True)))

		SpreadThreeStrategy(placement).select_host()

		placement.try_select.assert_called_once_with("regular", wait=False)

	def test_best_fit_packs_the_fuller_host(self) -> None:
		placement = self._placement(
			(self._host("less", used_memory_mib=8000), self._host("more", used_memory_mib=8800))
		)

		BestFitStrategy(placement).select_host()

		placement.try_select.assert_called_once_with("more", wait=False)

	def test_every_strategy_places_a_sleepy_request_on_a_sleepy_host(self) -> None:
		host = self._host("sleepy", is_sleepy=True, subscribed_memory_mib=8000)
		spread = self._placement((host,), is_sleepy=True, memory_mib=500, storage_mib=500)
		best_fit = self._placement((host,), is_sleepy=True, memory_mib=500, storage_mib=500)

		SpreadThreeStrategy(spread).select_host()
		BestFitStrategy(best_fit).select_host()

		spread.try_select.assert_called_once_with("sleepy", wait=False)
		best_fit.try_select.assert_called_once_with("sleepy", wait=False)

	def test_spread_selects_nothing_when_the_sleepy_pool_is_empty(self) -> None:
		placement = self._placement((self._host("regular"),), is_sleepy=True)

		SpreadThreeStrategy(placement).select_host()

		placement.try_select.assert_not_called()

	def test_existing_vm_stays_on_its_host_when_it_fits(self) -> None:
		placement = self._placement(
			(self._host("current", used_memory_mib=8000), self._host("empty")),
			action="start",
			current_host_name="current",
		)

		SpreadThreeStrategy(placement).select_host()

		placement.try_select.assert_called_once_with("current", wait=True)

	def test_a_resize_moves_when_the_current_host_has_no_room(self) -> None:
		placement = self._placement(
			(self._host("current", used_memory_mib=9500), self._host("other")),
			action="resize",
			current_host_name="current",
		)
		placement.try_select = Mock(side_effect=lambda host_name, wait=False: host_name == "other")

		BalancedStrategy(placement).select_host()

		self.assertEqual(
			placement.try_select.call_args_list, [call("current", wait=True), call("other", wait=False)]
		)

	def test_ranked_selection_queues_on_a_candidate_when_every_host_is_busy(self) -> None:
		hosts = (self._host("a"), self._host("b"), self._host("c"))
		placement = self._placement(hosts)
		placement.try_select = Mock(return_value=False)
		placement.has_contended_hosts = True
		placement.last_probe_was_contended = True
		strategy = BestFitStrategy(placement)

		strategy.select_from_ranked(hosts)

		waiting = [c for c in placement.try_select.call_args_list if c.kwargs.get("wait")]
		self.assertEqual(len(waiting), 1)
		self.assertIn(waiting[0].args[0], {"a", "b", "c"})

	def test_ranked_selection_does_not_queue_when_hosts_only_lacked_capacity(self) -> None:
		hosts = (self._host("a"), self._host("b"))
		placement = self._placement(hosts)
		placement.try_select = Mock(return_value=False)
		placement.has_contended_hosts = False

		BestFitStrategy(placement).select_from_ranked(hosts)

		self.assertFalse([c for c in placement.try_select.call_args_list if c.kwargs.get("wait")])

	def test_selection_retries_next_ranked_host(self) -> None:
		placement = self._placement((self._host("a"), self._host("b", used_memory_mib=1000)))
		placement.try_select.side_effect = [False, True]

		BestFitStrategy(placement).select_host()

		self.assertEqual(placement.try_select.call_args_list, [call("b", wait=False), call("a", wait=False)])

	def test_balanced_selects_the_only_fitting_host(self) -> None:
		placement = self._placement((self._host("available", used_storage_mib=7999),))

		BalancedStrategy(placement).select_host()

		placement.try_select.assert_called_once_with("available", wait=False)

	def test_balanced_keeps_a_fitting_lifecycle_change_on_current_host(self) -> None:
		placement = self._placement(
			(self._host("current", used_memory_mib=8000), self._host("other")),
			action="start",
			current_host_name="current",
		)

		BalancedStrategy(placement).select_host()

		placement.try_select.assert_called_once_with("current", wait=True)

	def test_one_pool_lets_a_sleepy_request_use_any_host(self) -> None:
		placement = self._placement((self._host("regular"),), is_sleepy=True)
		placement.use_dedicated_sleepy_vm_hosts = False

		BalancedStrategy(placement).select_host()

		placement.try_select.assert_called_once_with("regular", wait=False)

	def test_balanced_never_crosses_the_sleepy_pool_boundary(self) -> None:
		sleepy_request = self._placement((self._host("regular"),), is_sleepy=True)
		regular_request = self._placement((self._host("sleepy", is_sleepy=True),))

		BalancedStrategy(sleepy_request).select_host()
		BalancedStrategy(regular_request).select_host()

		sleepy_request.try_select.assert_not_called()
		regular_request.try_select.assert_not_called()

	def test_balanced_ranks_only_hosts_in_the_requested_pool(self) -> None:
		placement = self._placement(
			(self._host("regular", used_storage_mib=9000), self._host("sleepy", is_sleepy=True)),
			is_sleepy=True,
		)

		BalancedStrategy(placement).select_host()

		placement.try_select.assert_called_once_with("sleepy", wait=False)


class TestPlacementRetry(TestCase):
	"""Cover the retry that separates host lock contention from a full fleet."""

	@staticmethod
	def _requirements() -> PlacementRequirements:
		return PlacementRequirements(1000, 1024, 10240, "amd64", 7, False)

	@staticmethod
	def _placement_result(
		selected_host: str | None = None,
		*,
		contended: bool = False,
		probe_count: int = 0,
		remaining_seconds: float = PLACEMENT_DEADLINE_SECONDS,
	) -> Mock:
		return Mock(
			selected_host=selected_host,
			has_contended_hosts=contended,
			probe_count=probe_count,
			remaining_seconds=remaining_seconds,
			**{"try_select.return_value": bool(selected_host)},
		)

	@contextmanager
	def _patched(self, placements: list[Mock]):
		with (
			patch.object(PlacementStrategy, "_load_settings", return_value=("balanced", 1.0, True)),
			patch("atlas.vm.core.placement.strategies.base.is_pool_known_full", return_value=False),
			patch("atlas.vm.core.placement.strategies.base.remember_pool_is_full") as full,
			patch(
				"atlas.vm.core.placement.strategies.base.PlacementContext", side_effect=placements
			) as context,
			patch.object(PlacementStrategy, "bind_strategy"),
			patch.object(PlacementStrategy, "_wait_before_retry") as wait,
			patch("atlas.vm.core.placement.strategies.base.enqueue_capacity_expansion") as expansion,
		):
			yield context, wait, expansion, full

	def test_find_retries_while_candidate_hosts_are_locked(self) -> None:
		placements = [
			self._placement_result(contended=True),
			self._placement_result(contended=True),
			self._placement_result("a"),
		]
		with self._patched(placements) as (context, wait, expansion, _full):
			self.assertEqual(PlacementStrategy.find_server(self._requirements()), "a")

		self.assertEqual(context.call_count, 3)
		self.assertEqual([c.args[0] for c in wait.call_args_list], [0, 1])
		expansion.assert_not_called()

	def test_only_the_first_attempt_shares_a_cached_snapshot(self) -> None:
		placements = [self._placement_result(contended=True), self._placement_result("a")]
		with self._patched(placements) as (context, _wait, _expansion, _full):
			PlacementStrategy.find_server(self._requirements())

		self.assertEqual([c.kwargs["cache_snapshot"] for c in context.call_args_list], [True, False])

	def test_find_fails_on_the_first_attempt_when_the_fleet_is_full(self) -> None:
		placements = [self._placement_result(probe_count=0)]
		with self._patched(placements) as (context, wait, expansion, full):
			with self.assertRaises(OutOfCapacity):
				PlacementStrategy.find_server(self._requirements())

		self.assertEqual(context.call_count, 1)
		wait.assert_not_called()
		expansion.assert_called_once()
		full.assert_called_once()

	def test_a_pool_known_full_answers_without_any_placement_work(self) -> None:
		with (
			patch("atlas.vm.core.placement.strategies.base.is_pool_known_full", return_value=True),
			patch("atlas.vm.core.placement.strategies.base.PlacementContext") as context,
			patch.object(PlacementStrategy, "_load_settings", return_value=("balanced", 1.0)) as settings,
			patch("atlas.vm.core.placement.strategies.base.enqueue_capacity_expansion") as expansion,
		):
			with self.assertRaises(OutOfCapacity):
				PlacementStrategy.find_server(self._requirements())

		context.assert_not_called()
		settings.assert_not_called()
		expansion.assert_called_once()

	def test_a_resize_checks_its_current_host_when_the_pool_is_known_full(self) -> None:
		current_placement = CurrentPlacement("a", memory_mib=1024, disk_mib=1024)
		with (
			self._patched([self._placement_result("a")]) as (context, _wait, _expansion, _full),
			patch("atlas.vm.core.placement.strategies.base.is_pool_known_full", return_value=True),
		):
			self.assertEqual(
				PlacementStrategy.find_server(self._requirements(), current_placement=current_placement),
				"a",
			)

		self.assertIs(context.call_args.kwargs["current_placement"], current_placement)

	def test_pure_contention_never_marks_a_healthy_pool_full(self) -> None:
		placements = [self._placement_result(contended=True) for _ in range(PLACEMENT_ATTEMPTS)]
		with self._patched(placements) as (_context, _wait, _expansion, full):
			with self.assertRaises(PlacementBusy):
				PlacementStrategy.find_server(self._requirements())

		full.assert_not_called()

	def test_contention_is_reported_as_busy_and_buys_no_capacity(self) -> None:
		"""The fleet has room. Expanding it here spends money on a lock wait."""
		placements = [self._placement_result(contended=True) for _ in range(PLACEMENT_ATTEMPTS)]
		with self._patched(placements) as (_context, _wait, expansion, _full):
			with self.assertRaises(PlacementBusy) as raised:
				PlacementStrategy.find_server(self._requirements())

		expansion.assert_not_called()
		self.assertEqual(raised.exception.code, "placement_busy")
		self.assertEqual(raised.exception.http_status_code, 503)

	def test_a_full_fleet_is_reported_as_out_of_capacity_and_expands(self) -> None:
		placements = [self._placement_result(probe_count=0)]
		with self._patched(placements) as (_context, _wait, expansion, _full):
			with self.assertRaises(OutOfCapacity) as raised:
				PlacementStrategy.find_server(self._requirements())

		expansion.assert_called_once()
		self.assertEqual(raised.exception.code, "out_of_capacity")

	def test_an_expired_deadline_does_not_mark_an_unprobed_pool_full(self) -> None:
		placements = [self._placement_result(remaining_seconds=0)]
		with self._patched(placements) as (_context, _wait, _expansion, full):
			with self.assertRaises(OutOfCapacity):
				PlacementStrategy.find_server(self._requirements())

		full.assert_not_called()

	def test_probed_hosts_never_speak_for_the_whole_pool(self) -> None:
		placements = [self._placement_result(probe_count=PLACEMENT_PROBE_LIMIT)]
		with self._patched(placements) as (_context, _wait, _expansion, full):
			with self.assertRaises(OutOfCapacity):
				PlacementStrategy.find_server(self._requirements())

		full.assert_not_called()

	def test_a_placed_request_never_marks_the_pool_full(self) -> None:
		with self._patched([self._placement_result("a")]) as (_c, _w, expansion, full):
			self.assertEqual(PlacementStrategy.find_server(self._requirements()), "a")

		full.assert_not_called()
		expansion.assert_not_called()

	def test_find_gives_up_after_the_attempt_limit(self) -> None:
		placements = [self._placement_result(contended=True) for _ in range(PLACEMENT_ATTEMPTS)]
		with self._patched(placements) as (context, wait, expansion, _full):
			with self.assertRaises(PlacementBusy):
				PlacementStrategy.find_server(self._requirements())

		self.assertEqual(context.call_count, PLACEMENT_ATTEMPTS)
		self.assertEqual(wait.call_count, PLACEMENT_ATTEMPTS - 1)
		expansion.assert_not_called()

	def test_reserve_retries_a_contended_target_before_it_reports_no_capacity(self) -> None:
		placements = [self._placement_result(contended=True), self._placement_result("a")]
		with self._patched(placements) as (context, wait, _expansion, _full):
			self.assertEqual(PlacementStrategy.reserve_server(self._requirements(), "a"), "a")

		self.assertEqual(context.call_count, 2)
		self.assertEqual([c.args[0] for c in wait.call_args_list], [0])
