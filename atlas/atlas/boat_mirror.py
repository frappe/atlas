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
    liveness summary, the quarantined artifact sets, `observed_boat_version` —
    because placement and the operator read them per host and neither can afford
    a document parse to do it. Those totals are the
    *existing* `Refresh Capacity` fields, not a parallel set: the export keeps
    them live instead of them being a bootstrap-time snapshot, and a second set
    would only leave placement choosing which one it believed.
  - **The whole document archived as a `Host State Snapshot`**, keyed by host
    and observed epoch, for debugging, mirror rebuild and drift forensics.

One export is one transaction (`_apply`), so a mirror is never half a host.

Nothing pulls without a clock, so `sweep_mirrors` is the scheduled entry point
that refreshes every `boat_enabled` host — one enqueued job each, wired in
[hooks.py](../hooks.py). It is §2.6's *backstop*, deliberately: the `/watch` SSE
consumer that will carry low-latency deltas is a separate work order.

The rule that is easiest to get wrong is in `_freeze`, and it is the reason this
module has a failure path at all: **an unreachable Boat means the host is
Unknown, not dead.**

Its twin is in `_mirror_verdict`: **answering is not the same as being seen.** A
Boat that lost its store answers every request and holds no fence for anything, so
it will boot nothing it is asked to — `Unknown` is the honest word for that too,
and the export's `fence_epochs` map is where Atlas reads it.
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

# One host's refresh job deadline. The export is the only slow part and already
# carries its own; the ingest that follows is one short transaction. Twice the
# request's deadline covers it without letting a wedged job hold a `short` worker
# for minutes on end.
SYNC_JOB_TIMEOUT_SECONDS = EXPORT_TIMEOUT_SECONDS * 2

# Which hosts the sweep polls. `boat_enabled` is the switch — clearing it is the
# whole rollback, so a host without it must be poll-free as well as verb-free.
# Archived is the one status excluded: it names a retired host, and polling one
# forever would do nothing but flag a mirror nobody reads as Unknown. Draining and
# Broken are deliberately IN — a host in trouble is the host an operator most
# wants observed.
SWEEPABLE_HOSTS = {"boat_enabled": 1, "status": ("!=", "Archived")}

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
	raises for an unreachable host — see `HostMirror.sync`.

	Also the enqueued job body: `sweep_mirrors` schedules this same entry point per
	host, so the operator's button and the sweep cannot drift apart."""
	frappe.only_for("System Manager")
	return HostMirror(server).sync()


def sweep_mirrors() -> list[str]:
	"""Scheduled: refresh every Boat host's mirror. Returns the hosts enqueued.

	This is the whole of what keeps observed state observed. `PUT` desired is
	driven by an operator's click, but nothing clicks for the pull half — so
	without this sweep the mirror is whatever the last manual `sync_mirror` left
	behind, `Server.mirror_status` never turns Unknown for a host that has gone
	silent, and the capacity totals placement reads are a bootstrap-time snapshot
	again. The pull has to be on a clock.

	**Deliberately a periodic export and not a stream.** spec/33 §2.6 gives
	`GET /v1/watch` the low-latency deltas and makes the §2.5 export the
	truth-restoring backstop; a reader expecting the SSE consumer here will not
	find it, because it is a separate work order. The two are not alternatives —
	the backstop is what makes a dropped stream survivable — and a backstop that
	runs is worth more than a stream that does not.

	One enqueued job per host, never one loop over the fleet: a host that has gone
	quiet takes the full `EXPORT_TIMEOUT_SECONDS` to say so, and serially that is
	the sweep's whole budget spent on the hosts with the least to report — the
	fleet's healthy majority would go unpolled precisely when one host broke. The
	jobs are independent, so an unreachable Boat costs one worker one timeout and
	nothing else."""
	hosts = frappe.get_all("Server", filters=SWEEPABLE_HOSTS, pluck="name")
	return [server for server in hosts if enqueue_sync_mirror(server)]


def sync_job_id(server: str) -> str:
	"""The stable RQ job id for one host's refresh, so a sweep tick can never
	stack a second poll on top of one still waiting out its timeout."""
	return f"boat_sync_mirror::{server}"


def enqueue_sync_mirror(server: str) -> bool:
	"""Enqueue one host's mirror refresh. True if a job was queued, False if one
	was already in flight.

	Shaped after `worker.enqueue_finish_provisioning`, including the belt-and-braces
	dedup: `is_job_enqueued` answers for the caller (so the sweep can report what it
	actually did) and `deduplicate` closes the race between the check and the push.
	`short`, because a refresh is one bounded HTTP GET and one transaction — a host
	that cannot answer within its timeout has already told us what we needed to
	know."""
	from frappe.utils.background_jobs import is_job_enqueued

	if is_job_enqueued(sync_job_id(server)):
		return False
	frappe.enqueue(
		"atlas.atlas.boat_mirror.sync_mirror",
		queue="short",
		timeout=SYNC_JOB_TIMEOUT_SECONDS,
		job_id=sync_job_id(server),
		deduplicate=True,
		server=server,
	)
	return True


class HostMirror:
	"""One `Server` row's mirror of its Boat's observed state."""

	def __init__(self, server: str):
		self.server = server
		self._rows: dict[str, dict] | None = None

	def sync(self) -> dict:
		"""Pull `GET /export` and land it, or record why it could not be landed.

		Every path that does not land an export leaves the mirror exactly as it
		was. `boat_enabled` is checked first because clearing it is the whole
		rollback — a host without it behaves precisely as it did before Boat
		existed. A Fake-backed host is then never called at all, exactly as
		`run_task` gives it no SSH connection.

		The catch takes `frappe.ValidationError` as well as `BoatError` because
		`base_url_for_server` and `token_for_server` `frappe.throw` — a host with no
		mesh address or no token is unreachable in the only sense that matters here
		(Atlas cannot ask it anything), and it is only a different exception type
		because it is raised where credentials are resolved rather than on the
		socket. It reaches `_freeze` for the same reason everything else does: on a
		sweep tick, raising would be a traceback in a worker log every few minutes
		and no mark on the host, where freezing puts the sentence on the row.

		The try wraps **the request and the one read that decides whether the answer
		can be placed at all**, and nothing else — so an ingest that throws still
		throws, but a host that answers with a document Atlas cannot order lands on
		the row instead of escaping past the freeze. A document that cannot be
		ordered is a host that cannot be read, which is the same state as one that
		did not answer, and it belongs in the same place for the same reason."""
		if not boat_enabled(self.server):
			return self._rolled_back()
		if is_fake_server(self.server):
			return self._untouched("fake-host")
		try:
			export = self._client().get_export()
			epoch = self._epoch(export)
		except (BoatError, frappe.ValidationError) as error:
			return self._freeze(error)
		return self._ingest(export, epoch)

	def _client(self) -> BoatClient:
		return BoatClient.for_server(self.server, timeout_seconds=EXPORT_TIMEOUT_SECONDS)

	def _untouched(self, reason: str) -> dict:
		return {"server": self.server, "applied": False, "reason": reason}

	def _rolled_back(self) -> dict:
		"""This host is off Boat, so it has no mirror — and any claim left over from
		when it was on Boat is dropped here.

		`Server.validate` clears the pair when an operator unticks the box; this
		clears it for every OTHER way the flag goes off (a direct `set_value`, a
		patch, a fixture), which makes the operator's **Sync** button a repair
		instead of a second dead end. Without one of the two, a host frozen
		`Unknown` once and then rolled back was excluded from placement forever:
		the sweep skips it, so no export could ever clear the flag, and the field
		is read-only in the desk."""
		if frappe.db.get_value("Server", self.server, "mirror_status"):
			frappe.db.set_value(
				"Server", self.server, {"mirror_status": "", "mirror_error": ""}, update_modified=False
			)
		return self._untouched("boat-disabled")

	def _freeze(self, error: Exception) -> dict:
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
		# Bounded: `_error_sentence` falls back to a raw body, which can be a
		# proxy's whole HTML error page.
		return self._lose_sight(str(error), "unreachable")

	def _lose_sight(self, sentence: str, reason: str) -> dict:
		"""Flag the host `Unknown` with one sentence, and write nothing else.

		The single place the two mirror flags turn negative, so the rule `_freeze`
		states holds for every way of reaching it: no capacity total is nulled, no
		`Virtual Machine` row is touched, nothing is marked stopped and nothing is
		evicted."""
		frappe.db.set_value(
			"Server",
			self.server,
			{"mirror_status": "Unknown", "mirror_error": sentence[:1000]},
			update_modified=False,
		)
		return {
			"server": self.server,
			"applied": False,
			"reason": reason,
			"mirror_status": "Unknown",
			"error": sentence,
		}

	def _ingest(self, export: dict, epoch: int) -> dict:
		"""Land one export, ignore one already seen, or adopt one from a host whose
		store is not the one Atlas has been talking to.

		**Idempotency, stated once:** the observed epoch is monotonic, so the epoch
		the mirror already holds describes the same host state — nothing is
		re-landed and no snapshot row is duplicated. Freshness still is: a quiet
		host that answers is a host that has been SEEN, and freezing `observed_at`
		at the last change would report a healthy idle host as one nobody has heard
		from (`_reobserved`).

		**But the counter is per STORE, not per host** (spec/33 §3.1), and that is
		the distinction this method has to carry. A Boat that lost bbolt, was
		reinstalled, or was restored from a backup counts again from below the
		number Atlas holds. Read as nothing but a reordered poll, that host was
		never ingested again while Atlas went on reporting it `Fresh` and placing
		onto it — and §11.1 calls a Boat that lost its store the single most
		dangerous state the system can reach.

		The epoch alone cannot tell those two apart, so the **fences** do, which is
		what makes them worth reading: a late answer from a slow request comes from
		the store Atlas knows and still holds every fence it was given, while a lost
		store holds none. So a regression with the fences intact stays a no-op, and
		a regression from a host that has forgotten what it was told is adopted —
		the mirror must describe the host that exists now — and `_mirror_verdict`
		flags it on the way in."""
		mirrored = self._mirrored_epoch()
		if mirrored is None or epoch > mirrored:
			return self._apply(export, epoch)
		if epoch == mirrored:
			return self._reobserved(export, epoch)
		if _unfenced(self._fence_drift(export)):
			return self._apply(export, epoch)
		return {
			"server": self.server,
			"applied": False,
			"reason": "stale-epoch",
			"observed_epoch": epoch,
		}

	def _apply(self, export: dict, epoch: int) -> dict:
		"""Both landing places and the VM observations, in one transaction."""
		observed_at = _observed_at(export)
		frappe.db.savepoint(INGEST_SAVEPOINT)
		try:
			drift = self._stamp_virtual_machines(export)
			self._stamp_host(export, epoch, observed_at, _unfenced(drift))
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

	def _reobserved(self, export: dict, epoch: int) -> dict:
		"""The host answered with the snapshot the mirror already holds.

		Nothing on the host changed, so nothing is re-landed and the archive is not
		extended. What IS re-stated is the verdict: the host was reached, so a
		mirror frozen `Unknown` by an earlier failure recovers here rather than
		waiting for the host to happen to change. Freezing recovery behind "and it
		must also have moved since" is how a quiet host stays flagged forever."""
		self._stamp_host(export, epoch, _observed_at(export), _unfenced(self._fence_drift(export)))
		return {
			"server": self.server,
			"applied": False,
			"reason": "unchanged",
			"observed_epoch": epoch,
		}

	def _epoch(self, export: dict) -> int:
		"""The export's observed epoch.

		Required by the contract and the whole basis of ordering, so an export
		without one is a protocol surprise rather than a snapshot. It is raised
		inside `sync`'s try, where it becomes a flag on the row: a host answering
		a document Atlas cannot order is one Atlas cannot read, and on a sweep tick
		raising instead would be a worker traceback every few minutes with nothing
		an operator ever sees.

		**Zero is a legitimate epoch and telling it from absent is the point.** The
		counter starts at zero and is bumped on every observed CHANGE, so a
		brand-new Boat reports 0 until it first observes one — which is exactly the
		window between an operator enabling the flag and the host doing anything.
		Reading absent-as-zero rejected the export of every host in that window."""
		raw = export.get("observed_epoch")
		if raw is None:
			raise BoatError(f"Boat export for {self.server} carried no observed_epoch")
		try:
			epoch = int(raw)
		except (TypeError, ValueError) as error:
			raise BoatError(f"Boat export for {self.server} carried a non-numeric epoch {raw!r}") from error
		if epoch < 0:
			raise BoatError(f"Boat export for {self.server} carried a negative epoch {epoch}")
		return epoch

	def _mirrored_epoch(self) -> int | None:
		"""The epoch the mirror holds, or None when this host has never been
		ingested. The distinction only matters because zero is a real epoch: a host
		mirrored at 0 and a host never mirrored at all both read 0 out of the
		column, and conflating them left a fresh Boat's first export looking like
		one already seen."""
		row = frappe.db.get_value("Server", self.server, ["observed_at", "observed_epoch"], as_dict=True)
		if not row or not row.observed_at:
			return None
		return int(row.observed_epoch or 0)

	def _stamp_host(self, export: dict, epoch: int, observed_at, unfenced: list[str]) -> None:
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
			**_mirror_verdict(unfenced),
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
		# Quarantine is the one part of the export where an ABSENT key is a claim
		# and not a silence: Boat omits the array entirely when nothing is
		# quarantined, so absent means *none* and the field is written on every
		# ingest — including to clear it. That is the opposite of the host facts
		# above and of `units`, where absent means "not looked at"; reading it the
		# other way round would leave a host flagged forever for artifact sets an
		# operator has already cleaned up.
		values["observed_quarantined"] = _quarantined_identifiers(export)
		frappe.db.set_value("Server", self.server, values, update_modified=False)

	def _stamp_virtual_machines(self, export: dict) -> list[dict]:
		"""Write each VM's observed fields, and collect every disagreement.

		Drift is surfaced, never corrected: WO-1 is advisory, so the DB's
		`status` is not touched here and no lifecycle action is taken. The
		record goes onto the snapshot row, where an operator can diff one epoch
		against its neighbours.

		**Quarantine is read first, and its identifiers count as accounted for.**
		That ordering is the whole point of reading it: a half-terminated artifact
		set is invisible from the VM list by construction, so without it the VM
		Atlas has a row for would fall out the bottom of this method as ordinary
		`absent` drift — indistinguishable from a VM that was cleanly deleted, and
		the one reading that invites an operator to re-create or re-start it."""
		rows = self._atlas_rows()
		quarantine = _quarantine_records(export)
		quarantined = {record["identifier"] for record in quarantine}
		drift = [_quarantine_drift(rows.get(record["identifier"]), record) for record in quarantine]
		accounted_for = set(quarantined)
		for observed in export.get("virtual_machines") or []:
			uuid = observed.get("uuid")
			if not uuid or uuid in quarantined:
				continue
			accounted_for.add(uuid)
			drift.extend(self._absorb(rows.get(uuid), observed))
		drift.extend(_absent_drift(rows, accounted_for))
		drift.extend(self._fence_drift(export))
		return drift

	def _fence_drift(self, export: dict) -> list[dict]:
		"""Every disagreement between the fence Atlas issued for a VM and the one
		its host actually holds.

		The export ships `fence_epochs` keyed by UUID — "every fence this host
		holds" — and until now Atlas archived it unread while `_write_observation`
		claimed a disagreeing epoch "is reported as drift like any other
		disagreement". Nothing computed it, and it is the one comparison that says
		whether a host can do what Atlas has already asked of it: **a Boat refuses
		to boot a UUID it holds no fence for** (spec/33 §11.1), so a missing entry
		is not a bookkeeping nit — it is a VM that will not come back.

		Only VMs Atlas has actually FENCED and actually placed here are compared. A
		row with no `boot_epoch` was never issued one (every VM on a host still on
		SSH, until its first `PUT`), and comparing against a fence Atlas never
		issued would report the whole fleet as drifted the day a host is enabled.

		An export with no `fence_epochs` key at all reports nothing: unlike
		quarantine, absence here is silence rather than a claim — a Boat old enough
		not to send the map has not told Atlas it holds no fences (it always sends
		`{}` when it holds none, so the empty map IS the claim)."""
		fences = export.get("fence_epochs")
		if fences is None:
			return []
		drift = []
		for uuid, row in self._atlas_rows().items():
			if not row["boot_epoch"] or row["status"] not in PLACED_STATUSES:
				continue
			held = fences.get(uuid)
			if held == row["boot_epoch"]:
				continue
			drift.append(_drift(uuid, "fence", str(row["boot_epoch"]), None if held is None else str(held)))
		return drift

	def _absorb(self, row: dict | None, observed: dict) -> list[dict]:
		"""One exported VM against its row, if it has one. Never a quarantined
		one — the caller has already taken those out."""
		uuid = observed["uuid"]
		if row is None:
			# The host runs a VM this host's Atlas rows do not account for.
			# Recorded, never adopted: enrolment is Atlas's (spec/33 §1).
			return [_drift(uuid, "unenrolled", None, _observed_status(observed))]
		self._write_observation(uuid, observed)
		return _row_drift(row, observed)

	def _write_observation(self, uuid: str, observed: dict) -> None:
		"""The observed status, and only that. `status` is the DB's.

		`boot_epoch` is deliberately NOT written back. Atlas is the sole issuer of
		the fence (spec/33 §11.1), and an issuer that adopts the number its host
		reports is not an issuer. A Boat restored from a backup, or one adopting a
		host it has never fenced, would otherwise hand Atlas an epoch Atlas never
		granted and see it re-stated as authoritative from then on — leaving no
		record of what was actually issued, and turning the one mechanism that
		prevents two live copies of a VM into a value the host chooses.

		An epoch Boat holds that disagrees with the one Atlas issued is drift, and
		is reported as drift like any other disagreement — computed in
		`_fence_drift` from the export's own `fence_epochs` map, which is the only
		channel that can say a host holds NO fence for a VM."""
		frappe.db.set_value(
			"Virtual Machine",
			uuid,
			{"observed_status": _observed_status(observed)},
			update_modified=False,
		)

	def _atlas_rows(self) -> dict[str, dict]:
		"""Every VM Atlas places on this host, by UUID. Read once per sync — the
		VM comparison, the fence comparison and the regression test all ask the
		same question of the same rows."""
		if self._rows is None:
			rows = frappe.get_all(
				"Virtual Machine",
				filters={"server": self.server},
				fields=["name", "status", "desired_power", "boot_epoch"],
			)
			self._rows = {row["name"]: row for row in rows}
		return self._rows


def _unfenced(drift: list[dict]) -> list[str]:
	"""The VMs whose host holds no fence for them at all — the subset of fence drift
	that is not a disagreement but an absence. A host holding a DIFFERENT epoch still
	knows the VM; a host holding none will boot nothing when asked (spec/33 §11.1)."""
	return [row["virtual_machine"] for row in drift if row["kind"] == "fence" and row["observed"] is None]


def _mirror_verdict(unfenced: list[str]) -> dict:
	"""`mirror_status` / `mirror_error` for a host that answered.

	**`Fresh` is not the automatic reward for answering.** A Boat that lost its
	bbolt file answers perfectly well, reports its VMs, and holds no fence for any of
	them — so it will boot NOTHING it is asked to (spec/33 §11.1 names that the most
	dangerous state the system can reach), and reported `Fresh` it went on receiving
	every new arrival. So the verdict asks the same question `_freeze` asks, and
	spells the answer with the same word: can Atlas still take this host at its word?
	A host that cannot run what Atlas already placed on it cannot, so it reads
	`Unknown` and `placement.placement_candidates` stops filling it.

	Nothing is evicted, exactly as for an unreachable host (§9): the VMs it holds are
	running right now, and the fence governs only the NEXT boot. And it needs no
	operator to clear — every lifecycle verb re-`PUT`s desired state and
	`assert_desired_state` is the explicit repair, so the fences come back and the
	next export reads `Fresh` again."""
	if not unfenced:
		return {"mirror_status": "Fresh", "mirror_error": ""}
	named = ", ".join(sorted(unfenced)[:5])
	return {
		"mirror_status": "Unknown",
		"mirror_error": (
			f"Boat holds no fence for {len(unfenced)} VM(s) placed here ({named}): its store was lost, "
			f"reinstalled or restored from a backup, so it will boot none of them until desired state "
			f"is re-asserted."
		)[:1000],
	}


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


def _absent_drift(rows: dict[str, dict], accounted_for: set) -> list[dict]:
	"""VMs Atlas places on this host that the host neither reported nor quarantined.

	`accounted_for` is deliberately wider than the exported VM list. A quarantined
	artifact set is the host saying "this identifier is here and I cannot read it",
	which is the opposite of absent; reporting it twice — once quarantined, once
	missing — would leave the operator acting on whichever they read second."""
	return [
		_drift(uuid, "absent", row["status"], None)
		for uuid, row in rows.items()
		if uuid not in accounted_for and row["status"] in PLACED_STATUSES
	]


def _quarantine_records(export: dict) -> list[dict]:
	"""Every artifact set this host holds that Boat could not read as a coherent
	VM, as `{identifier, reason}` (spec/33 §3.4).

	Read from the export's **top-level `quarantine` array**, which is where Boat
	puts them. It has to be a top-level array rather than a per-VM flag, because a
	quarantined artifact set is not in the VM list at all — a half-terminated VM is
	invisible from that list by construction — and a host reporting no VMs and a
	host reporting no VMs plus three quarantined artifact sets must not be the same
	document.

	`identifier`, not `uuid`: it is usually a VM UUID, but a stranded namespace or
	address keeps only its own name, and inventing a UUID for it would record a
	guess as a fact — which is the thing quarantine exists to refuse. One with no
	identifier at all cannot be named to an operator or matched to a row, so it is
	dropped here; the archived document still carries it verbatim.

	The per-VM `quarantined` flag is folded into the same set. The contract no
	longer declares it — `api/openapi.yaml` now lists `quarantined` /
	`quarantine_reason` among the fields deliberately absent from `VirtualMachine`,
	on the grounds that the host scan keeps the two sets disjoint by construction —
	but it was in the shipped schema until that change, and a Boat fleet runs mixed
	versions by design (§5 rolls a version canary-first and rolls a failed host back
	to N-1). An Atlas that ignored a host still sending it would write an observed
	status for a half-deleted VM, and a half-deleted VM ingested as truth is a VM
	Atlas will try to start. Reading both channels costs one loop; it stops finding
	anything on its own the day no host sends it. The array wins a tie: it is the
	channel that can name an artifact set with no UUID, and the one with the
	evidence."""
	records: dict[str, str] = {}
	for record in export.get("quarantine") or []:
		records.setdefault((record.get("identifier") or "").strip(), record.get("reason") or "")
	for observed in export.get("virtual_machines") or []:
		if observed.get("quarantined"):
			records.setdefault((observed.get("uuid") or "").strip(), observed.get("quarantine_reason") or "")
	records.pop("", None)
	return [{"identifier": identifier, "reason": reason} for identifier, reason in records.items()]


def _quarantine_drift(row: dict | None, record: dict) -> dict:
	"""One quarantined artifact set, stated in the drift vocabulary.

	`desired` is what Atlas believes that identifier is (None when Atlas has no row
	for it — the quarantine analogue of `unenrolled`), `observed` is Boat's
	one-sentence reason. Drift is reused rather than given a second channel because
	it is already the mirror's word for "the host and the DB disagree", and because
	it lands quarantine on the `Host State Snapshot` row and in its `drift_count`
	for free: an operator diffing one epoch against its neighbours finds the
	artifact set in the same place as every other disagreement, with the evidence
	still in the archived document beside it."""
	return _drift(record["identifier"], "quarantined", row["status"] if row else None, record["reason"])


def _quarantined_identifiers(export: dict) -> str:
	"""The quarantined artifact sets for the `Server` row, comma-separated.

	Denormalized for the same reason `observed_units_down` is — it is read per host
	on a health sweep, and the detail stays in the archived document — and NOT as a
	placement input. Quarantine is unresolved leftovers on a host Atlas can see
	perfectly well; it says nothing about the host's capacity, and draining a host
	over one stale LV would be a policy Atlas invented for itself."""
	return ", ".join(record["identifier"] for record in _quarantine_records(export))


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
