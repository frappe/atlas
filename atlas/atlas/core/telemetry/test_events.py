from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.telemetry import events
from atlas.atlas.core.telemetry.events import (
	count,
	observe,
	read_counters,
	restore_observations,
	take_observations,
	timed,
)


class PlacementBusy(Exception):
	pass


class TestTelemetryEvents(UnitTestCase):
	def setUp(self) -> None:
		frappe.cache.delete(
			frappe.cache.make_key(events.COUNTERS_KEY), frappe.cache.make_key(events.OBSERVATIONS_KEY)
		)

	def test_counts_of_one_series_add_up_in_any_label_order(self) -> None:
		count("atlas_api_requests_total", operation="create_virtual_machine", status="201")
		count("atlas_api_requests_total", 2, status="201", operation="create_virtual_machine")

		(sample,) = read_counters("2026-10-14T00:00:00+00:00")
		self.assertEqual(sample["value"], 3)
		self.assertEqual(sample["labels"], {"operation": "create_virtual_machine", "status": "201"})

	def test_a_label_that_names_one_virtual_machine_is_rejected(self) -> None:
		with self.assertRaises(ValueError):
			observe("atlas_placement_duration_seconds", 0.1, virtual_machine="vm-1-0001")

	def test_timed_names_the_failure_and_still_raises(self) -> None:
		with self.assertRaises(PlacementBusy):
			with timed("atlas_placement_duration_seconds"):
				raise PlacementBusy

		(observation,) = take_observations()
		self.assertEqual(observation["labels"], {"result": "placement_busy"})

	def test_restored_observations_keep_the_newest_within_the_bound(self) -> None:
		observe("atlas_migration_duration_seconds", 30, result="completed")
		pending = take_observations()
		self.assertEqual(take_observations(), [])

		with patch.object(events, "MAXIMUM_OBSERVATIONS", 2):
			observe("atlas_migration_duration_seconds", 40, result="completed")
			observe("atlas_migration_duration_seconds", 50, result="completed")
			restore_observations(pending)

		self.assertEqual([sample["value"] for sample in take_observations()], [40, 50])
