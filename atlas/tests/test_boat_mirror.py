"""Unit tests for the Boat mirror — Atlas ingesting a host's observed state
(spec/33 §1, §2.5, §9; WO-1).

No daemon runs here: `requests.request` is patched throughout, which is also the
point. These prove that one export lands in BOTH places, that re-ingesting is a
no-op, that drift is reported rather than repaired, and — the rule this module
exists to get right — that an unreachable Boat freezes the mirror instead of
nulling it and never touches a VM's status.

The wire helpers and the two host fixtures are imported from `test_boat_client`:
both files cover the same seam, and a second `_Response` would be a second
opinion about what Boat's wire looks like.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from atlas.atlas import boat_mirror
from atlas.atlas.doctype.host_state_snapshot import host_state_snapshot as snapshot_module
from atlas.tests import fixtures
from atlas.tests.test_boat_client import (
	REQUEST,
	_boat_host_token,
	_boat_server,
	_clear_virtual_machines,
	_patch_conf,
	_Response,
)

MIRROR_FIELDS = (
	"observed_epoch",
	"observed_at",
	"observed_boat_version",
	"mirror_status",
	"mirror_error",
	"observed_units_down",
	"observed_quarantined",
	"vcpus_total",
	"memory_megabytes_total",
	"pool_disk_gigabytes_total",
)


def _host_facts(**overrides) -> dict:
	"""A HostFacts document shaped like `api/openapi.yaml`'s schema."""
	facts = {
		"hostname": "atlas-host-1",
		"boat_version": "0.1.0",
		"kernel_version": "6.8.0-31-generic",
		"firecracker_version": "1.7.0",
		"vcpus_total": 8,
		"memory_megabytes_total": 16384,
		"memory_megabytes_free": 9000,
		"pool_disk_gigabytes_total": 300,
		"pool_used_percent": 41.5,
		"host_signature": "sig-abc",
	}
	facts.update(overrides)
	return facts


def _observed(uuid: str, status: str = "Running", **overrides) -> dict:
	"""A VirtualMachine observed document."""
	document = {
		"uuid": uuid,
		"observed_status": status,
		"observed_at": "2026-07-27T10:00:00Z",
		"boot_epoch": 3,
	}
	document.update(overrides)
	return document


def _quarantine(identifier: str, reason: str = "half-terminated", **overrides) -> dict:
	"""A Quarantine document shaped like `api/openapi.yaml`'s schema. `identifier`,
	not `uuid`: a stranded namespace or address keeps only its own name."""
	record = {
		"identifier": identifier,
		"reason": reason,
		"evidence": ["unit is active", "no network namespace"],
		"seen_at": "2026-07-27T10:00:00Z",
	}
	record.update(overrides)
	return record


def _export(epoch: int = 5, virtual_machines: list | None = None, **overrides) -> dict:
	export = {
		"observed_epoch": epoch,
		"taken_at": "2026-07-27T10:00:00Z",
		"host": _host_facts(),
		"virtual_machines": virtual_machines or [],
	}
	export.update(overrides)
	return export


class _MirrorTestCase(IntegrationTestCase):
	"""One real-provider host with Boat switched on, and one VM placed on it."""

	def setUp(self) -> None:
		self.server = _boat_server()
		self.image = fixtures.make_image("boat-mirror-image")
		_clear_virtual_machines()
		frappe.db.delete("Host State Snapshot")
		# The mirror is reset, not just the flag: `IntegrationTestCase` rolls back
		# per CLASS and the Server fixture is get-or-create by title, so an epoch a
		# previous test landed is still on the row for the next one — which then
		# reads its export as already seen and applies nothing.
		frappe.db.set_value(
			"Server",
			self.server.name,
			{
				"boat_enabled": 1,
				"observed_epoch": 0,
				"mirror_status": "",
				"mirror_error": "",
				"observed_quarantined": "",
			},
			update_modified=False,
		)
		self.virtual_machine = fixtures.make_virtual_machine(self.server.name, self.image.name)
		self._set_status("Running")

	def _set_status(self, status: str, name: str | None = None) -> None:
		frappe.db.set_value(
			"Virtual Machine", name or self.virtual_machine.name, "status", status, update_modified=False
		)

	def _sync(self, export: dict) -> dict:
		with _boat_host_token(self.server.name), patch(REQUEST, return_value=_Response(payload=export)):
			return boat_mirror.sync_mirror(self.server.name)

	def _server_value(self, field: str):
		return frappe.db.get_value("Server", self.server.name, field)

	def _mirror(self) -> dict:
		return frappe.db.get_value("Server", self.server.name, MIRROR_FIELDS, as_dict=True)

	def _virtual_machine_value(self, field: str, name: str | None = None):
		return frappe.db.get_value("Virtual Machine", name or self.virtual_machine.name, field)

	def _snapshots(self) -> list[dict]:
		return frappe.get_all(
			"Host State Snapshot",
			filters={"server": self.server.name},
			fields=["name", "observed_epoch", "drift_count", "drift", "export_document"],
			order_by="observed_epoch desc",
		)


class TestExportLandsInBothPlaces(_MirrorTestCase):
	"""§2.5: hot fields on `Server` for placement, the whole document archived."""

	def test_hot_fields_are_denormalized_onto_the_server(self) -> None:
		self._sync(_export(epoch=5, virtual_machines=[_observed(self.virtual_machine.name)]))

		mirror = self._mirror()
		self.assertEqual(mirror["observed_epoch"], 5)
		self.assertEqual(mirror["observed_boat_version"], "0.1.0")
		self.assertEqual(mirror["mirror_status"], "Fresh")
		# The host's own taken_at (stamped UTC per the contract), not ingest time.
		self.assertEqual(
			frappe.utils.get_datetime(mirror["observed_at"]),
			frappe.utils.convert_utc_to_system_timezone(datetime(2026, 7, 27, 10, 0, tzinfo=UTC)).replace(
				tzinfo=None
			),
		)

	def test_capacity_reuses_the_refresh_capacity_fields(self) -> None:
		# The export keeps the EXISTING totals live; it does not add a second,
		# disagreeing set for placement to choose between.
		self._sync(_export())

		mirror = self._mirror()
		self.assertEqual(mirror["vcpus_total"], 8)
		self.assertEqual(mirror["memory_megabytes_total"], 16384)
		self.assertEqual(mirror["pool_disk_gigabytes_total"], 300)
		self.assertEqual(self._server_value("pool_data_percent"), 41.5)
		self.assertIsNotNone(self._server_value("capacity_reported_at"))
		self.assertEqual(self._server_value("kernel_version"), "6.8.0-31-generic")

	def test_unit_liveness_lands_as_the_units_that_are_down(self) -> None:
		units = [
			{"name": "boat.service", "active_state": "active", "sub_state": "running"},
			{"name": "atlas-networkd.service", "active_state": "failed", "sub_state": "failed"},
		]
		self._sync(_export(units=units))

		self.assertEqual(self._server_value("observed_units_down"), "atlas-networkd.service")

	def test_the_whole_document_is_archived_keyed_by_host_and_epoch(self) -> None:
		export = _export(epoch=9, virtual_machines=[_observed(self.virtual_machine.name)])
		result = self._sync(export)

		snapshots = self._snapshots()
		self.assertEqual(len(snapshots), 1)
		self.assertEqual(snapshots[0]["observed_epoch"], 9)
		self.assertEqual(snapshots[0]["name"], result["snapshot"])
		# Verbatim: the archive is for forensics, so nothing the host said is
		# dropped on the way in — including the facts Atlas deliberately does
		# not denormalize.
		self.assertEqual(json.loads(snapshots[0]["export_document"]), export)

	def test_observation_lands_on_the_vm_and_status_is_left_alone(self) -> None:
		self._sync(_export(virtual_machines=[_observed(self.virtual_machine.name, "Stopped", boot_epoch=7)]))

		self.assertEqual(self._virtual_machine_value("observed_status"), "Stopped")
		# WO-1 is advisory: the DB is still authoritative.
		self.assertEqual(self._virtual_machine_value("status"), "Running")

	def test_the_host_can_never_dictate_the_fence_epoch(self) -> None:
		"""Atlas is the sole issuer of the fence (spec/33 §11.1), and an issuer
		that adopts the number its host reports is not an issuer.

		A Boat restored from a backup — or one adopting a host it has never been
		given a fence for — reports an epoch Atlas never granted. Writing it back
		would leave Atlas with no record of what it actually issued, and would
		turn the one mechanism that prevents two live copies of a VM into a value
		the host chooses for itself."""
		frappe.db.set_value("Virtual Machine", self.virtual_machine.name, "boot_epoch", 4)

		self._sync(_export(virtual_machines=[_observed(self.virtual_machine.name, "Stopped", boot_epoch=7)]))

		self.assertEqual(self._virtual_machine_value("boot_epoch"), 4)

	def test_an_absent_fence_leaves_the_epoch_alone(self) -> None:
		observed = _observed(self.virtual_machine.name)
		del observed["boot_epoch"]
		self._sync(_export(virtual_machines=[observed]))

		# Boat holding no fence is not Atlas issuing epoch 0.
		self.assertEqual(self._virtual_machine_value("boot_epoch"), 0)
		self.assertEqual(self._virtual_machine_value("observed_status"), "Running")

	def test_a_status_atlas_does_not_know_is_recorded_as_unknown(self) -> None:
		self._sync(_export(virtual_machines=[_observed(self.virtual_machine.name, "Paused")]))

		self.assertEqual(self._virtual_machine_value("observed_status"), "Unknown")

	def test_a_missing_fact_never_nulls_what_was_measured(self) -> None:
		self._sync(_export(epoch=1))
		self._sync(_export(epoch=2, host={"hostname": "atlas-host-1", "boat_version": "0.2.0"}))

		mirror = self._mirror()
		self.assertEqual(mirror["observed_boat_version"], "0.2.0")
		self.assertEqual(mirror["vcpus_total"], 8)
		self.assertEqual(mirror["memory_megabytes_total"], 16384)


class TestIngestIsIdempotent(_MirrorTestCase):
	"""The observed epoch orders exports, and that ordering is the whole rule."""

	def test_re_ingesting_the_same_epoch_changes_nothing(self) -> None:
		export = _export(epoch=4, virtual_machines=[_observed(self.virtual_machine.name)])
		first = self._sync(export)
		second = self._sync(export)

		self.assertTrue(first["applied"])
		self.assertFalse(second["applied"])
		self.assertEqual(second["reason"], "stale-epoch")
		self.assertEqual(len(self._snapshots()), 1)

	def test_an_older_epoch_is_ignored(self) -> None:
		self._sync(_export(epoch=8, virtual_machines=[_observed(self.virtual_machine.name, "Running")]))
		result = self._sync(
			_export(epoch=3, virtual_machines=[_observed(self.virtual_machine.name, "Stopped")])
		)

		self.assertFalse(result["applied"])
		self.assertEqual(self._server_value("observed_epoch"), 8)
		# A late answer from a slow request must not overwrite a newer one.
		self.assertEqual(self._virtual_machine_value("observed_status"), "Running")
		self.assertEqual([row["observed_epoch"] for row in self._snapshots()], [8])

	def test_an_export_without_an_epoch_cannot_be_ordered_and_raises(self) -> None:
		export = _export()
		del export["observed_epoch"]
		with self.assertRaises(boat_mirror.BoatError):
			self._sync(export)

		self.assertEqual(self._snapshots(), [])

	def test_retention_bounds_the_archived_snapshots(self) -> None:
		with patch.object(snapshot_module, "SNAPSHOTS_KEPT_PER_HOST", 3):
			for epoch in range(1, 6):
				self._sync(_export(epoch=epoch))

		self.assertEqual([row["observed_epoch"] for row in self._snapshots()], [5, 4, 3])


class TestUnreachableBoatFreezesTheMirror(_MirrorTestCase):
	"""§9: the host is Unknown, NOT dead. A partitioned host is still running
	every one of its VMs, so nothing here may read as "this host has no VMs"."""

	def setUp(self) -> None:
		super().setUp()
		self._sync(_export(epoch=6, virtual_machines=[_observed(self.virtual_machine.name, "Running")]))
		self.before = self._mirror()

	def _sync_failing(self, side_effect=None, response=None) -> dict:
		with (
			_boat_host_token(self.server.name),
			patch(REQUEST, side_effect=side_effect, return_value=response),
		):
			return boat_mirror.sync_mirror(self.server.name)

	def test_an_unreachable_daemon_flags_the_mirror_unknown(self) -> None:
		result = self._sync_failing(side_effect=requests.ConnectionError("connection refused"))

		self.assertFalse(result["applied"])
		self.assertEqual(result["reason"], "unreachable")
		self.assertEqual(self._server_value("mirror_status"), "Unknown")
		self.assertIn("connection refused", self._server_value("mirror_error"))

	def test_the_frozen_mirror_keeps_its_last_values(self) -> None:
		self._sync_failing(side_effect=requests.ConnectionError("connection refused"))

		after = self._mirror()
		for field in ("observed_epoch", "observed_at", "observed_boat_version", "vcpus_total"):
			self.assertEqual(after[field], self.before[field], field)
		self.assertEqual(after["memory_megabytes_total"], self.before["memory_megabytes_total"])
		self.assertEqual(after["pool_disk_gigabytes_total"], self.before["pool_disk_gigabytes_total"])

	def test_no_vm_is_marked_stopped_or_otherwise_touched(self) -> None:
		self._sync_failing(side_effect=requests.ConnectionError("connection refused"))

		self.assertEqual(self._virtual_machine_value("status"), "Running")
		self.assertEqual(self._virtual_machine_value("observed_status"), "Running")

	def test_the_archive_is_not_extended_by_a_failed_poll(self) -> None:
		self._sync_failing(side_effect=requests.ConnectionError("connection refused"))

		self.assertEqual([row["observed_epoch"] for row in self._snapshots()], [6])

	def test_a_broken_daemon_is_unknown_too_not_an_empty_host(self) -> None:
		# A 500 is Boat answering badly, which says as little about firecracker
		# as silence does.
		response = _Response(status_code=500, payload={"error": "bbolt is wedged"})
		self._sync_failing(response=response)

		self.assertEqual(self._server_value("mirror_status"), "Unknown")
		self.assertEqual(self._server_value("observed_epoch"), 6)
		self.assertEqual(self._virtual_machine_value("observed_status"), "Running")

	def test_a_host_with_no_credentials_is_unknown_too_not_a_traceback(self) -> None:
		# A host Atlas cannot address or authenticate to is unreachable in the only
		# sense that matters. On a scheduled sweep, raising it would be a worker
		# traceback every tick and nothing on the row an operator would ever see.
		with _patch_conf({"atlas_boat_tokens": None, "atlas_boat_token": None}), patch(REQUEST) as request:
			result = boat_mirror.sync_mirror(self.server.name)

		request.assert_not_called()
		self.assertEqual(result["reason"], "unreachable")
		self.assertIn("atlas_boat_tokens", self._server_value("mirror_error"))
		self.assertEqual(self._server_value("observed_epoch"), 6)

	def test_a_later_successful_export_clears_the_flag(self) -> None:
		self._sync_failing(side_effect=requests.ConnectionError("connection refused"))
		self._sync(_export(epoch=7, virtual_machines=[_observed(self.virtual_machine.name)]))

		self.assertEqual(self._server_value("mirror_status"), "Fresh")
		self.assertEqual(self._server_value("mirror_error"), "")


class TestDriftIsSurfacedNotCorrected(_MirrorTestCase):
	"""Atlas reports drift in WO-1; it does not act on it."""

	def _drift(self, export: dict) -> list[dict]:
		return self._sync(export)["drift"]

	def test_a_disagreement_about_status_is_recorded_not_repaired(self) -> None:
		drift = self._drift(_export(virtual_machines=[_observed(self.virtual_machine.name, "Stopped")]))

		self.assertEqual(
			drift,
			[
				{
					"virtual_machine": self.virtual_machine.name,
					"kind": "status",
					"desired": "Running",
					"observed": "Stopped",
				}
			],
		)
		self.assertEqual(self._virtual_machine_value("status"), "Running")
		self.assertEqual(self._snapshots()[0]["drift_count"], 1)

	def test_desired_power_against_the_observed_power(self) -> None:
		frappe.db.set_value(
			"Virtual Machine", self.virtual_machine.name, "desired_power", "Running", update_modified=False
		)
		self._set_status("Stopped")
		drift = self._drift(_export(virtual_machines=[_observed(self.virtual_machine.name, "Stopped")]))

		self.assertEqual([row["kind"] for row in drift], ["power"])
		self.assertEqual(drift[0]["desired"], "Running")

	def test_a_sleeping_vm_still_satisfies_desired_running(self) -> None:
		frappe.db.set_value(
			"Virtual Machine", self.virtual_machine.name, "desired_power", "Running", update_modified=False
		)
		self._set_status("Sleeping")
		drift = self._drift(_export(virtual_machines=[_observed(self.virtual_machine.name, "Sleeping")]))

		self.assertEqual(drift, [])

	def test_boat_not_knowing_is_ignorance_not_drift(self) -> None:
		drift = self._drift(_export(virtual_machines=[_observed(self.virtual_machine.name, "Unknown")]))

		self.assertEqual(drift, [])
		self.assertEqual(self._virtual_machine_value("observed_status"), "Unknown")

	def test_a_placed_vm_the_host_never_mentions_is_drift(self) -> None:
		drift = self._drift(_export(virtual_machines=[]))

		self.assertEqual([row["kind"] for row in drift], ["absent"])
		self.assertEqual(drift[0]["virtual_machine"], self.virtual_machine.name)

	def test_a_vm_atlas_has_no_row_for_is_reported_never_adopted(self) -> None:
		drift = self._drift(
			_export(
				virtual_machines=[
					_observed(self.virtual_machine.name),
					_observed("11111111-2222-3333-4444-555555555555"),
				]
			)
		)

		self.assertEqual([row["kind"] for row in drift], ["unenrolled"])
		self.assertFalse(frappe.db.exists("Virtual Machine", "11111111-2222-3333-4444-555555555555"))

	def test_an_unprovisioned_vm_is_not_reported_missing(self) -> None:
		self._set_status("Pending")
		drift = self._drift(_export(virtual_machines=[]))

		self.assertEqual(drift, [])


class TestQuarantineIsReportedNeverIngested(_MirrorTestCase):
	"""§3.4: artifact sets on the host that Boat could not read as a coherent VM —
	a crash part-way through a terminate, an LV with no VM directory, a unit with
	no `network.env`.

	Boat reports them in the export's own **top-level `quarantine` array**, and it
	has to be top-level: a quarantined artifact set is not in the VM list at all —
	a half-terminated VM is invisible from that list by construction — so without
	the array, a host reporting no VMs and a host reporting no VMs plus three
	quarantined artifact sets would be the same document."""

	def _drift(self, export: dict) -> list[dict]:
		return self._sync(export)["drift"]

	def test_the_top_level_array_is_read_and_reported_as_drift(self) -> None:
		drift = self._drift(
			_export(quarantine=[_quarantine(self.virtual_machine.name, "rootfs LV gone, unit remains")])
		)

		self.assertEqual([row["kind"] for row in drift], ["quarantined"])
		self.assertEqual(drift[0]["virtual_machine"], self.virtual_machine.name)
		self.assertEqual(drift[0]["desired"], "Running")
		self.assertIn("rootfs LV gone", drift[0]["observed"])
		# Riding the drift list is what puts it on the snapshot row for free, where
		# an operator diffs one epoch against its neighbours.
		self.assertEqual(self._snapshots()[0]["drift_count"], 1)

	def test_it_is_never_ingested_as_an_observation(self) -> None:
		self._sync(_export(quarantine=[_quarantine(self.virtual_machine.name)]))

		# A half-deleted VM ingested as truth is a VM Atlas will try to start —
		# a guest booted onto a disk the controller already released.
		self.assertFalse(self._virtual_machine_value("observed_status"))
		self.assertEqual(self._virtual_machine_value("status"), "Running")

	def test_a_quarantined_vm_is_not_reported_merely_absent(self) -> None:
		"""The defect this class exists for. The VM is missing from the export's VM
		list by construction, so with the array unread it arrived as ordinary
		`absent` drift — indistinguishable from a VM that was cleanly deleted, which
		is the reading under which an operator re-creates or re-starts it."""
		drift = self._drift(_export(virtual_machines=[], quarantine=[_quarantine(self.virtual_machine.name)]))

		self.assertEqual([row["kind"] for row in drift], ["quarantined"])

	def test_an_artifact_set_with_no_vm_row_is_reported_too(self) -> None:
		"""The identifier need not be a UUID: a stranded namespace keeps only its own
		name, and inventing a UUID for it would record a guess as a fact — which is
		what quarantine exists to refuse."""
		drift = self._drift(
			_export(
				virtual_machines=[_observed(self.virtual_machine.name)],
				quarantine=[_quarantine("atlas-ns-orphan", "namespace with no VM directory")],
			)
		)

		self.assertEqual([row["kind"] for row in drift], ["quarantined"])
		self.assertEqual(drift[0]["virtual_machine"], "atlas-ns-orphan")
		self.assertIsNone(drift[0]["desired"])
		# The healthy VM beside it is absorbed exactly as before.
		self.assertEqual(self._virtual_machine_value("observed_status"), "Running")

	def test_the_identifiers_land_on_the_server_row(self) -> None:
		self._sync(_export(quarantine=[_quarantine("orphan-a"), _quarantine("orphan-b")]))

		self.assertEqual(self._server_value("observed_quarantined"), "orphan-a, orphan-b")

	def test_an_export_with_no_quarantine_clears_the_row(self) -> None:
		"""Quarantine is the one part of the export whose ABSENCE is a claim: Boat
		omits the array when there is nothing to report. Read like a host fact —
		where a missing value leaves the last one standing — a host would stay
		flagged forever for artifact sets an operator had already cleaned up."""
		self._sync(_export(epoch=1, quarantine=[_quarantine("orphan-a")]))
		self._sync(_export(epoch=2))

		self.assertEqual(self._server_value("observed_quarantined"), "")

	def test_a_host_with_quarantine_is_not_a_host_with_nothing(self) -> None:
		healthy = [_observed(self.virtual_machine.name)]
		quiet = self._sync(_export(epoch=1, virtual_machines=healthy))
		messy = self._sync(_export(epoch=2, virtual_machines=healthy, quarantine=[_quarantine("orphan-a")]))

		self.assertEqual(quiet["drift"], [])
		self.assertEqual([row["kind"] for row in messy["drift"]], ["quarantined"])

	def test_the_per_vm_flag_is_honoured_as_well(self) -> None:
		"""The contract has since dropped `quarantined` from `VirtualMachine`, but it
		was in the shipped schema and a Boat fleet runs mixed versions by design
		(spec/33 §5 canaries a version and rolls a failed host back to N-1). An Atlas
		that ignored a host still sending it would write an observed status for a
		half-deleted VM, so both channels feed one set."""
		drift = self._drift(
			_export(
				virtual_machines=[
					_observed(
						self.virtual_machine.name,
						"Stopped",
						quarantined=True,
						quarantine_reason="unit is active with no namespace",
					)
				]
			)
		)

		self.assertEqual([row["kind"] for row in drift], ["quarantined"])
		self.assertFalse(self._virtual_machine_value("observed_status"))
		self.assertEqual(self._server_value("observed_quarantined"), self.virtual_machine.name)


class TestTheSweepIsWhatMakesTheMirrorLive(_MirrorTestCase):
	"""The scheduled pull (spec/33 §2.5). Atlas pushes desired state when an
	operator clicks; nothing clicks for the pull half, so without this sweep every
	observed field on every host is whatever the last manual refresh left."""

	def setUp(self) -> None:
		super().setUp()
		self.enqueued: list[dict] = []

	def _sweep(self, in_flight: bool = False) -> list[str]:
		with (
			patch("frappe.utils.background_jobs.is_job_enqueued", return_value=in_flight),
			patch("frappe.enqueue", side_effect=lambda method, **kwargs: self.enqueued.append(kwargs)),
		):
			return boat_mirror.sweep_mirrors()

	def _host(self, title: str, **overrides) -> "frappe.model.document.Document":
		overrides.setdefault("status", "Active")
		return fixtures.make_server(fixtures.make_provider("boat-test-provider"), title, **overrides)

	def test_the_sweep_is_registered_in_the_scheduler(self) -> None:
		# The defect this class exists for: `sync_mirror` had no caller anywhere, so
		# the whole observed-state path only ever ran inside its own tests.
		from atlas import hooks

		cron_jobs = [job for jobs in hooks.scheduler_events.get("cron", {}).values() for job in jobs]
		self.assertIn("atlas.atlas.boat_mirror.sweep_mirrors", cron_jobs)

	def test_only_boat_enabled_hosts_are_swept(self) -> None:
		ssh_host = self._host("boat-mirror-ssh-host", boat_enabled=0)

		swept = self._sweep()

		self.assertIn(self.server.name, swept)
		# Clearing the flag is the whole rollback: an SSH host must be poll-free as
		# well as verb-free, or the mirror would keep flagging a host nobody drives.
		self.assertNotIn(ssh_host.name, swept)

	def test_a_retired_host_is_not_polled_forever(self) -> None:
		archived = self._host("boat-mirror-archived-host", boat_enabled=1, status="Archived")

		self.assertNotIn(archived.name, self._sweep())

	def test_a_host_in_trouble_is_the_one_most_worth_observing(self) -> None:
		broken = self._host("boat-mirror-broken-host", boat_enabled=1, status="Broken")

		self.assertIn(broken.name, self._sweep())

	def test_each_host_gets_its_own_job(self) -> None:
		"""One job per host, never one loop over the fleet: a silent host takes the
		full export timeout to say so, and serially that is the sweep's whole budget
		spent on the hosts with the least to report."""
		second = self._host("boat-mirror-second-host", boat_enabled=1)

		swept = self._sweep()

		self.assertIn(second.name, swept)
		servers = [job["server"] for job in self.enqueued]
		self.assertEqual(sorted(servers), sorted(set(servers)))
		self.assertEqual(len(self.enqueued), len(swept))
		self.assertEqual(
			{job["job_id"] for job in self.enqueued},
			{boat_mirror.sync_job_id(server) for server in swept},
		)

	def test_a_poll_still_in_flight_is_never_stacked(self) -> None:
		# A host that has gone quiet holds its job for the whole export timeout,
		# which is longer than the gap between two sweep ticks.
		self.assertEqual(self._sweep(in_flight=True), [])
		self.assertEqual(self.enqueued, [])

	def test_the_job_is_the_entry_point_the_operator_clicks(self) -> None:
		with (
			patch("frappe.utils.background_jobs.is_job_enqueued", return_value=False),
			patch("frappe.enqueue") as enqueue,
		):
			boat_mirror.enqueue_sync_mirror(self.server.name)

		self.assertEqual(enqueue.call_args.args[0], "atlas.atlas.boat_mirror.sync_mirror")
		self.assertEqual(enqueue.call_args.kwargs["queue"], "short")
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
		self.assertEqual(enqueue.call_args.kwargs["timeout"], boat_mirror.SYNC_JOB_TIMEOUT_SECONDS)

	def test_one_unreachable_host_does_not_starve_the_others(self) -> None:
		"""The reason the sweep may enqueue rather than loop, proved on the job body:
		a host that does not answer records a state and returns. If it raised, the
		next host's poll would be the exception handler's problem."""
		quiet = self._host("boat-mirror-quiet-host", boat_enabled=1)
		answers = [requests.ConnectionError("connection refused"), _Response(payload=_export(epoch=3))]

		def _answer(*args, **kwargs):
			answer = answers.pop(0)
			if isinstance(answer, Exception):
				raise answer
			return answer

		tokens = {"atlas_boat_tokens": {quiet.name: "s3cret", self.server.name: "s3cret"}}
		with _patch_conf({**tokens, "atlas_boat_token": None}), patch(REQUEST, side_effect=_answer):
			silent, answered = (boat_mirror.sync_mirror(name) for name in (quiet.name, self.server.name))

		self.assertEqual(silent["reason"], "unreachable")
		self.assertEqual(frappe.db.get_value("Server", quiet.name, "mirror_status"), "Unknown")
		self.assertTrue(answered["applied"])
		self.assertEqual(self._server_value("mirror_status"), "Fresh")


class TestOffByDefault(_MirrorTestCase):
	"""With the flag clear, and on a Fake host, nothing is called and nothing
	is written — the WO-0 rollback covers WO-1 unchanged."""

	def test_a_host_without_boat_enabled_is_never_called(self) -> None:
		frappe.db.set_value("Server", self.server.name, "boat_enabled", 0, update_modified=False)
		with _boat_host_token(self.server.name), patch(REQUEST) as request:
			result = boat_mirror.sync_mirror(self.server.name)

		request.assert_not_called()
		self.assertEqual(result["reason"], "boat-disabled")
		self.assertFalse(self._server_value("mirror_status"))

	def test_a_fake_host_is_never_called_and_needs_no_credentials(self) -> None:
		provider = fixtures.make_provider_row("boat-mirror-fake-provider", provider_type="Fake")
		fixtures.set_atlas_settings(provider)
		server = fixtures.make_server(
			provider,
			"boat-mirror-fake-server",
			ipv4_address="203.0.113.45",
			status="Active",
			boat_enabled=1,
		)
		with (
			_patch_conf({"atlas_boat_tokens": None, "atlas_boat_token": None}),
			patch(REQUEST) as request,
		):
			result = boat_mirror.sync_mirror(server.name)

		request.assert_not_called()
		self.assertEqual(result["reason"], "fake-host")
		self.assertFalse(frappe.db.get_value("Server", server.name, "mirror_status"))
