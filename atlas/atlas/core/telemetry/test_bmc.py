from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.telemetry.bmc import BMCCollector

LOG = "/redfish/v1/Systems/1/LogServices/SEL"


def entry(entry_id: str, severity: str = "Critical", age: timedelta = timedelta(hours=1), **fields) -> dict:
	created = (datetime.now(UTC) - age).isoformat()
	return {
		"Id": entry_id,
		"Severity": severity,
		"Created": created,
		"Message": f"event {entry_id}",
		**fields,
	}


class TestBMCLogEntries(UnitTestCase):
	def setUp(self) -> None:
		provider = SimpleNamespace(get_bmc_client=Mock())
		self.server = SimpleNamespace(
			name="bmc-test-host", settings=SimpleNamespace(server_provider_controller=provider)
		)
		frappe.cache.delete_value(f"atlas:telemetry:bmc_seen_entries:{self.server.name}")

	def read(self, entries: list[dict], clock_offset: timedelta = timedelta()) -> list[dict]:
		collector = BMCCollector(self.server)
		collector.clock_offset = clock_offset
		collector.add_new_log_entries(LOG, "SEL", entries)
		frappe.cache.set_value(collector.seen_entries_key, collector.current_entries)
		return collector.lines

	def read_entry_ids(self, entries: list[dict]) -> list[str]:
		return [line["attributes"]["entry_id"] for line in self.read(entries)]

	def test_the_first_read_sends_only_recent_problems(self) -> None:
		sent = self.read_entry_ids(
			[
				entry("1"),
				entry("2", age=timedelta(days=40)),
				entry("3", severity="OK"),
				entry(
					"4", Links={"OriginOfCondition": {"@odata.id": "/redfish/v1/Systems/1/LogServices/SMI"}}
				),
			]
		)

		self.assertEqual(sent, ["1"])

	def test_a_later_read_sends_new_and_updated_entries_once(self) -> None:
		first = entry("1")
		self.read_entry_ids([first])

		# iLO updates a repeated event in place.
		# A clock reset can give a new event an old timestamp.
		entries = [{**first, "Updated": datetime.now(UTC).isoformat()}, entry("2", age=timedelta(days=40))]

		updated, _ = self.read(entries)
		self.assertEqual(updated["ts"], entries[0]["Updated"])
		self.assertEqual(self.read_entry_ids(entries), [])

	def test_an_entry_time_is_corrected_by_the_bmc_clock_offset(self) -> None:
		# The BMC clock is 8 hours behind.
		bmc_time = entry("1", age=timedelta(hours=9))

		[line] = self.read([bmc_time], clock_offset=-timedelta(hours=8))

		age = datetime.now(UTC) - datetime.fromisoformat(line["ts"])
		self.assertAlmostEqual(age.total_seconds(), timedelta(hours=1).total_seconds(), delta=60)
		self.assertEqual(line["attributes"]["created"], bmc_time["Created"])
