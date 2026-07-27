"""Atlas's read-through mirror of one host's observed state (spec/33, WO-1).

Boat is authoritative for what a host actually is; Atlas is authoritative for
what it should be. This module is the one direction that did not exist before:
`GET /v1/export` in, and Boat's fact landed beside Atlas's intent.

**The mirror is disposable and is never authority.** Losing every byte of it
costs one export round trip, and nothing is ever rebuilt from it. That is what
makes WO-1 safe to ship advisory-only: the DB still decides, and this module
only reports. Concretely, nothing below writes `Virtual Machine.status`, moves a
placement, stops a VM, or evicts anything — where the DB and the host disagree,
the disagreement is recorded as **drift** and left standing (spec/33 §1).

The export lands in two places, deliberately both (§2.5):

  - **Hot fields denormalized onto `Server`** — the capacity totals, the unit
    liveness summary, `observed_boat_version` — because placement queries them
    on every provision and cannot afford a document parse. Those totals are the
    *existing* `Refresh Capacity` fields, not a parallel set: the export keeps
    them live instead of them being a bootstrap-time snapshot, and a second set
    would only leave placement choosing which one it believed.
  - **The whole document archived as a `Host State Snapshot`**, keyed by host
    and observed epoch, for debugging, mirror rebuild and drift forensics.

One export is one transaction (`_apply`), so a mirror is never half a host.

The rule that is easiest to get wrong is in `_freeze`, and it is the reason this
module has a failure path at all: **an unreachable Boat means the host is
Unknown, not dead.**
"""

from __future__ import annotations

from datetime import datetime

import frappe

from atlas.atlas.boat_client import BoatClient, BoatError, boat_enabled
from atlas.atlas.doctype.host_state_snapshot.host_state_snapshot import HostStateSnapshot
from atlas.atlas.providers.fake_tasks import is_fake_server

# The savepoint the ingest runs inside. The mirror joins the caller's
# transaction rather than committing its own — a sweep over the fleet, a Desk
# click and a background job all have one open already — but it must still land
# whole or not at all, or a `Server` row could claim an epoch whose snapshot and
# VM observations were never written.
INGEST_SAVEPOINT = "boat_mirror_ingest"

# The export is one short read on Boat's side and must stay a bounded request:
# a slow read is exactly how a busy host gets mis-declared partitioned, and a
# false Unknown is a false everything (spec/33 §11.6).
EXPORT_TIMEOUT_SECONDS = 30

# The statuses `Virtual Machine.observed_status` allows. Anything else is
# recorded as Unknown — the mirror never invents an observation.
OBSERVED_STATUSES = ("Running", "Stopped", "Sleeping", "Unknown", "Failed")

# Which DB statuses an observed status agrees with. A Paused VM's unit is still
# active, so the host sees it Running: `VirtualMachineStatus` in the contract
# carries no Paused, and reading Paused as disagreement would report drift on
# every paused VM in the fleet.
STATUS_AGREEMENT = {
	"Running": ("Running", "Paused"),
	"Stopped": ("Stopped",),
	"Sleeping": ("Sleeping",),
	"Failed": ("Failed",),
}

# The statuses that assert the VM exists on its host, so its absence from the
# export is drift. A Pending VM was never provisioned and a Failed one may never
# have landed, so neither is expected to be there; a Terminated one is caught by
# the status comparison instead, if the host still reports it.
PLACED_STATUSES = ("Running", "Paused", "Stopped", "Sleeping")

# Host facts that land ON the Server row: exported name -> Server fieldname.
# The capacity trio is deliberately the same three fields Refresh Capacity
# stamps, and `pool_used_percent` lands on the same `pool_data_percent` the
# server-facts Task fills.
#
# Deliberately NOT landed, though both stay in the archived document:
# `memory_megabytes_free`, because Atlas computes capacity from the VM rows on
# the Sleeping axis (`api/server_capacity.py`) and a host-reported free number
# could only be a second, disagreeing answer to the same question; and
# `host_signature`, which is a per-snapshot warm-restore guard rather than a
# placement input.
HOST_FACTS_TO_SERVER = {
	"boat_version": "observed_boat_version",
	"vcpus_total": "vcpus_total",
	"memory_megabytes_total": "memory_megabytes_total",
	"pool_disk_gigabytes_total": "pool_disk_gigabytes_total",
	"pool_used_percent": "pool_data_percent",
	"kernel_version": "kernel_version",
	"firecracker_version": "firecracker_version",
}

# The subset of those that is a capacity measurement, so `capacity_reported_at`
# only moves when the host actually re-measured.
CAPACITY_FACTS = ("vcpus_total", "memory_megabytes_total", "pool_disk_gigabytes_total")


@frappe.whitelist()
def sync_mirror(server: str) -> dict:
	"""Refresh one host's mirror from its Boat. Returns what happened, never
	raises for an unreachable host — see `HostMirror.sync`."""
	frappe.only_for("System Manager")
	return HostMirror(server).sync()


class HostMirror:
	"""One `Server` row's mirror of its Boat's observed state."""

	def __init__(self, server: str):
		self.server = server

	def sync(self) -> dict:
		"""Pull `GET /export` and land it, or record why it could not be landed.

		Every path that does not land an export leaves the mirror exactly as it
		was. `boat_enabled` is checked first because clearing it is the whole
		rollback — a host without it behaves precisely as it did before Boat
		existed. A Fake-backed host is then never called at all, exactly as
		`run_task` gives it no SSH connection."""
		if not boat_enabled(self.server):
			return self._untouched("boat-disabled")
		if is_fake_server(self.server):
			return self._untouched("fake-host")
		try:
			export = self._client().get_export()
		except BoatError as error:
			return self._freeze(error)
		return self._ingest(export)

	def _client(self) -> BoatClient:
		return BoatClient.for_server(self.server, timeout_seconds=EXPORT_TIMEOUT_SECONDS)

	def _untouched(self, reason: str) -> dict:
		return {"server": self.server, "applied": False, "reason": reason}

	def _freeze(self, error: BoatError) -> dict:
		"""Boat did not answer. **The host is Unknown, not dead** (spec/33 §9).

		Stated as code because it is the rule this module exists to get right:
		the only writes here are the two mirror flags. Nothing nulls the
		capacity totals, nothing touches a `Virtual Machine` row, nothing marks
		a VM stopped and nothing evicts anything.

		An unreachable daemon is evidence about the daemon, not about
		firecracker — a partitioned host is still running every one of its VMs.
		A nulled mirror would read as "this host has no VMs", which is precisely
		the input placement and capacity accounting would then act on, turning a
		management-plane blip into a fleet-wide outage. So the stale mirror
		freezes at its last observed values and is flagged stale here.

		This boundary deliberately records rather than raises: a partition is a
		*state* Atlas must hold about the host, not an operation that failed.
		The failure is still loud — it is on the row, in the operator's face,
		with the daemon's own sentence."""
		frappe.db.set_value(
			"Server",
			self.server,
			# Bounded: `_error_sentence` falls back to a raw body, which can be a
			# proxy's whole HTML error page.
			{"mirror_status": "Unknown", "mirror_error": str(error)[:1000]},
			update_modified=False,
		)
		return {
			"server": self.server,
			"applied": False,
			"reason": "unreachable",
			"mirror_status": "Unknown",
			"error": str(error),
		}

	def _ingest(self, export: dict) -> dict:
		"""Land one export, or ignore it as already seen.

		**Idempotency, stated once:** the observed epoch is monotonic per host,
		so an epoch the mirror already holds is the same snapshot and an older
		one is a reordered poll. Both are no-ops — re-ingesting must not
		duplicate a snapshot row, and a late answer from a slow request must
		never overwrite a newer one."""
		epoch = self._epoch(export)
		if epoch <= self._mirrored_epoch():
			return {
				"server": self.server,
				"applied": False,
				"reason": "stale-epoch",
				"observed_epoch": epoch,
			}
		return self._apply(export, epoch)

	def _apply(self, export: dict, epoch: int) -> dict:
		"""Both landing places and the VM observations, in one transaction."""
		observed_at = _observed_at(export)
		frappe.db.savepoint(INGEST_SAVEPOINT)
		try:
			drift = self._stamp_virtual_machines(export)
			self._stamp_host(export, epoch, observed_at)
			snapshot = HostStateSnapshot.record(self.server, export, observed_at, drift)
		except Exception:
			frappe.db.rollback(save_point=INGEST_SAVEPOINT)
			raise
		frappe.db.release_savepoint(INGEST_SAVEPOINT)
		return {
			"server": self.server,
			"applied": True,
			"observed_epoch": epoch,
			"snapshot": snapshot.name,
			"virtual_machines": len(export.get("virtual_machines") or []),
			"drift": drift,
		}

	def _epoch(self, export: dict) -> int:
		"""The export's observed epoch.

		Required by the contract and the whole basis of ordering: an export
		without one cannot be placed against the mirror, so it is a protocol
		surprise rather than a snapshot. Raise instead of guessing, exactly as
		`boat_client._outcome` does for a non-terminal operation."""
		epoch = int(export.get("observed_epoch") or 0)
		if epoch <= 0:
			raise BoatError(f"Boat export for {self.server} carried no observed_epoch")
		return epoch

	def _mirrored_epoch(self) -> int:
		return int(frappe.db.get_value("Server", self.server, "observed_epoch") or 0)

	def _stamp_host(self, export: dict, epoch: int, observed_at) -> None:
		"""Denormalize the hot fields onto `Server` — one UPDATE, no document
		lifecycle.

		`frappe.db.set_value` rather than `doc.save()` on purpose: a mirror
		refresh is an observation, not a business change, and saving the row
		would fire `Server.on_update` and report a Central event on every poll.
		`update_modified=False` for the same reason — `observed_at` already
		records freshness, and moving `modified` would put every host at the top
		of every recently-changed view once a minute."""
		facts = export.get("host") or {}
		values = {
			"observed_epoch": epoch,
			"observed_at": observed_at,
			"mirror_status": "Fresh",
			"mirror_error": "",
		}
		# Only facts the export actually carried: a missing fact must leave the
		# last known value standing, never null it (same rule as `_freeze`).
		for exported, field in HOST_FACTS_TO_SERVER.items():
			if facts.get(exported) is not None:
				values[field] = facts[exported]
		if any(facts.get(fact) is not None for fact in CAPACITY_FACTS):
			values["capacity_reported_at"] = frappe.utils.now_datetime()
		if export.get("units") is not None:
			values["observed_units_down"] = _units_down(export["units"])
		frappe.db.set_value("Server", self.server, values, update_modified=False)

	def _stamp_virtual_machines(self, export: dict) -> list[dict]:
		"""Write each VM's observed fields, and collect every disagreement.

		Drift is surfaced, never corrected: WO-1 is advisory, so the DB's
		`status` is not touched here and no lifecycle action is taken. The
		record goes onto the snapshot row, where an operator can diff one epoch
		against its neighbours."""
		rows = self._atlas_rows()
		drift: list[dict] = []
		observed_uuids = set()
		for observed in export.get("virtual_machines") or []:
			uuid = observed.get("uuid")
			if not uuid:
				continue
			observed_uuids.add(uuid)
			drift.extend(self._absorb(rows.get(uuid), observed))
		drift.extend(_absent_drift(rows, observed_uuids))
		return drift

	def _absorb(self, row: dict | None, observed: dict) -> list[dict]:
		"""One exported VM against its row, if it has one."""
		uuid = observed["uuid"]
		if row is None:
			# The host runs a VM this host's Atlas rows do not account for.
			# Recorded, never adopted: enrolment is Atlas's (spec/33 §1).
			return [_drift(uuid, "unenrolled", None, _observed_status(observed))]
		if observed.get("quarantined"):
			# Artifacts Boat could not read as a coherent state — a crash
			# part-way through a terminate. Reported, never ingested as truth,
			# because a half-deleted VM ingested as truth is a VM Atlas will try
			# to start (spec/33 §3.4).
			return [_drift(uuid, "quarantined", row["status"], observed.get("quarantine_reason") or "")]
		self._write_observation(uuid, observed)
		return _row_drift(row, observed)

	def _write_observation(self, uuid: str, observed: dict) -> None:
		"""The two observed fields, and only those. `status` is the DB's."""
		values = {"observed_status": _observed_status(observed)}
		if observed.get("boot_epoch") is not None:
			# Only when Boat reports holding a fence. An absent epoch means Boat
			# holds none and will refuse to boot the VM — writing 0 for that
			# would be indistinguishable from Atlas having issued epoch 0, and
			# from WO-2 it would clobber the issuer's value.
			values["boot_epoch"] = int(observed["boot_epoch"])
		frappe.db.set_value("Virtual Machine", uuid, values, update_modified=False)

	def _atlas_rows(self) -> dict[str, dict]:
		"""Every VM Atlas places on this host, by UUID."""
		rows = frappe.get_all(
			"Virtual Machine",
			filters={"server": self.server},
			fields=["name", "status", "desired_power"],
		)
		return {row["name"]: row for row in rows}


def _row_drift(row: dict, observed: dict) -> list[dict]:
	"""Every way one VM's row and its host disagree, stated once each."""
	status = _observed_status(observed)
	if status == "Unknown":
		# Boat saying it does not know is ignorance, not disagreement.
		return []
	drift = []
	if row["status"] not in STATUS_AGREEMENT.get(status, ()):
		drift.append(_drift(row["name"], "status", row["status"], status))
	if row.get("desired_power") and row["desired_power"] != _power_of(status):
		drift.append(_drift(row["name"], "power", row["desired_power"], status))
	return drift


def _absent_drift(rows: dict[str, dict], observed_uuids: set) -> list[dict]:
	"""VMs Atlas places on this host that the host did not report."""
	return [
		_drift(uuid, "absent", row["status"], None)
		for uuid, row in rows.items()
		if uuid not in observed_uuids and row["status"] in PLACED_STATUSES
	]


def _drift(uuid: str, kind: str, desired: str | None, observed: str | None) -> dict:
	return {"virtual_machine": uuid, "kind": kind, "desired": desired, "observed": observed}


def _power_of(status: str) -> str:
	"""The `desired_power` an observed status satisfies. Sleeping satisfies
	Running: a sleeping VM is parked and wakes on traffic, not powered off
	(spec/32)."""
	return "Running" if status in ("Running", "Sleeping") else "Stopped"


def _observed_status(observed: dict) -> str:
	"""Boat's status in the vocabulary the field allows.

	A status Atlas does not know is recorded as Unknown rather than written
	through. `Paused` is the live example: spec/33 §1 lists it in the observed
	vocabulary but `VirtualMachineStatus` in the contract does not carry it, so
	a Boat that grew it would arrive here unannounced."""
	status = observed.get("observed_status")
	return status if status in OBSERVED_STATUSES else "Unknown"


def _units_down(units: list[dict]) -> str:
	"""The supervised units that were not active, comma-separated."""
	return ", ".join(unit.get("name", "") for unit in units if unit.get("active_state") != "active")


def _observed_at(export: dict):
	"""The export's `taken_at` as a naive datetime in the site's timezone.

	The host stamps it in UTC per the contract's `date-time` format. An
	unparseable or missing stamp falls back to now: the timestamp is a freshness
	breadcrumb, and refusing a whole export over it would trade the mirror for
	its own metadata."""
	raw = (export.get("taken_at") or "").strip()
	if not raw:
		return frappe.utils.now_datetime()
	try:
		stamped = datetime.fromisoformat(raw.replace("Z", "+00:00"))
	except ValueError:
		return frappe.utils.now_datetime()
	if stamped.tzinfo is None:
		return stamped
	return frappe.utils.convert_utc_to_system_timezone(stamped).replace(tzinfo=None)
