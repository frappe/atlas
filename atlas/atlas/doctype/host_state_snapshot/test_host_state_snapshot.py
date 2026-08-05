"""Unit tests for the Host State Snapshot archive (spec/33 §2.5, WO-1).

What is proven here is only what makes the mirror disposable: a snapshot is
keyed by (host, observed epoch), retention is bounded per host, and the bound
keeps the NEWEST epochs rather than the newest inserts — a replayed backlog must
not evict the present.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.host_state_snapshot import host_state_snapshot as module
from atlas.atlas.doctype.host_state_snapshot.host_state_snapshot import (
	SNAPSHOTS_KEPT_PER_HOST,
	HostStateSnapshot,
)
from atlas.tests import fixtures


def _export(epoch: int, boat_version: str = "0.1.0", virtual_machines: list | None = None) -> dict:
	"""An Export document shaped like `api/openapi.yaml`'s schema."""
	return {
		"observed_epoch": epoch,
		"taken_at": "2026-07-27T10:00:00Z",
		"host": {"hostname": "atlas-host-1", "boat_version": boat_version},
		"virtual_machines": virtual_machines if virtual_machines is not None else [],
	}


class TestHostStateSnapshot(IntegrationTestCase):
	def setUp(self) -> None:
		provider = fixtures.make_provider("snapshot-test-provider")
		self.server = fixtures.make_server(provider, "snapshot-test-server", status="Active")
		self.other = fixtures.make_server(provider, "snapshot-other-server", status="Active")
		frappe.db.delete("Host State Snapshot")

	def _record(self, epoch: int, server: str | None = None, drift: list | None = None):
		return HostStateSnapshot.record(
			server or self.server.name,
			_export(epoch),
			frappe.utils.now_datetime(),
			drift or [],
		)

	def _epochs(self, server: str | None = None) -> list[int]:
		return frappe.get_all(
			"Host State Snapshot",
			filters={"server": server or self.server.name},
			order_by="observed_epoch desc",
			pluck="observed_epoch",
		)

	def test_record_archives_the_whole_document_against_its_host(self) -> None:
		snapshot = self._record(7, drift=[{"virtual_machine": "vm-1", "kind": "status"}])

		self.assertEqual(snapshot.server, self.server.name)
		self.assertEqual(snapshot.observed_epoch, 7)
		self.assertEqual(snapshot.boat_version, "0.1.0")
		self.assertEqual(snapshot.drift_count, 1)
		self.assertIn("vm-1", snapshot.drift)
		self.assertIn("atlas-host-1", snapshot.export_document)

	def test_retention_keeps_the_newest_epochs_per_host(self) -> None:
		for epoch in range(1, SNAPSHOTS_KEPT_PER_HOST + 4):
			self._record(epoch)

		kept = self._epochs()
		self.assertEqual(len(kept), SNAPSHOTS_KEPT_PER_HOST)
		self.assertEqual(kept[0], SNAPSHOTS_KEPT_PER_HOST + 3)
		self.assertEqual(kept[-1], 4)

	def test_a_replayed_old_epoch_never_evicts_the_present(self) -> None:
		# Ordering is by the HOST's epoch, not by insert order: a backlog
		# replayed after a partition must fall off the end, not push the newest
		# snapshot out of the window.
		with patch.object(module, "SNAPSHOTS_KEPT_PER_HOST", 2):
			for epoch in (10, 11, 1):
				self._record(epoch)

			self.assertEqual(self._epochs(), [11, 10])

	def test_retention_is_per_host(self) -> None:
		with patch.object(module, "SNAPSHOTS_KEPT_PER_HOST", 2):
			for epoch in (1, 2, 3):
				self._record(epoch)
			self._record(1, server=self.other.name)

			self.assertEqual(self._epochs(), [3, 2])
			self.assertEqual(self._epochs(self.other.name), [1])
