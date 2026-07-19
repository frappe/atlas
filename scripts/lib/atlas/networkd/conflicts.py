"""Conflict detection + operator event hook (spec §7.3 / §18.2).

The conflict detector itself lives in `records.effective_ownership` — the
/128 in two origins' active sets goes to `OwnershipTable.conflicts` and is
dropped from routing. Stage 5 adds the operator-visible surface: every
conflict START and END emits an event the operator wires to alerting (the spec
deliberately leaves the alerting stack out — this module just emits).

A small file-based event sink at `/var/lib/atlas-networkd/conflicts.jsonl`
appends one JSON line per event (start / end) so an operator can `tail -F` it
locally or ship via their existing log pipeline. A process-in-memory hook
(`ConflictEvents.subscribe`) lets a test register a callback directly — used
by the test of the detector + the daemon's metrics counter.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .records import IP6, HostID, OwnershipTable

_DEFAULT_CONFLICTS_LOG = "/var/lib/atlas-networkd/conflicts.jsonl"


@dataclass(frozen=True, slots=True)
class ConflictEvent:
	"""One transition: a /128 entered (`kind="start"`) or left
	(`kind="end"`) the conflicting state. `origins` is the set of origin
	HostIDs whose latest advertisements all include this /128 at the moment of
	the transition."""

	kind: str
	private_ip: IP6
	origins: frozenset[HostID]
	at: float  # the wall-clock timestamp (seconds, monotonic in tests)


@dataclass(slots=True)
class ConflictTracker:
	"""Tracks the previous-conflicts set so we can emit END events when a
	conflict clears. Driven by the loop once per scan / apply tick (a /128's
	conflict status only changes when the effective table changes, i.e. on an
	advertisement apply). Cheap: O(|conflicts|).

	`now_fn` (NOT prefixed with `_`) is the public injection point — tests
	pass a controlled clock; production wires `time.time` (the events carry
	wall-clock timestamps for an operator log)."""

	_prev: frozenset[IP6] = field(default_factory=frozenset)
	_subscribers: list[Callable[[ConflictEvent], None]] = field(default_factory=list)
	now_fn: Callable[[], float] = field(default=time.time)
	_log_path: str | None = _DEFAULT_CONFLICTS_LOG
	# The origins we recorded at the previous conflict state — so an END event
	# knows which origins were contesting the /128 that just cleared.
	_prev_origins: dict[IP6, frozenset[HostID]] = field(default_factory=dict)

	def observe(self, table: OwnershipTable, latest_per_origin: dict | None = None) -> list[ConflictEvent]:
		"""Compare the new effective table's conflicts to the previous one,
		emit START events for new conflicts and END events for cleared ones,
		persist any subscribers' callbacks. Returns the events emitted this
		observed transition.

		``latest_per_origin`` must be the ``{origin: OwnershipAdvertisement}``
		dict that produced `table`; it is used to populate the ``origins`` field
		on each event. If omitted (no caller provides it today), the events
		carry empty origin sets — correct only for tests that don't inspect
		that field."""
		now = self.now_fn()
		new = table.conflicts
		new_origins: dict[IP6, frozenset[HostID]] = {}
		for ip in new:
			origins = frozenset(
				origin for origin, adv in (latest_per_origin or {}).items() if ip in adv.owned
			)
			new_origins[ip] = origins
		started = new - self._prev
		ended = self._prev - new
		events: list[ConflictEvent] = []
		for ip in sorted(started):
			ev = ConflictEvent("start", ip, new_origins[ip], now)
			events.append(ev)
		for ip in sorted(ended):
			ev = ConflictEvent("end", ip, self._prev_origins.get(ip, frozenset()), now)
			events.append(ev)
		self._prev = new
		self._prev_origins = new_origins
		for ev in events:
			for cb in self._subscribers:
				cb(ev)
			self._append_log(ev)
		return events

	def subscribe(self, cb: Callable[[ConflictEvent], None]) -> None:
		"""Register an in-process callback. Used by tests + by the daemon to
		wire a metrics counter incr on `start` / decr on `end`."""
		self._subscribers.append(cb)

	def _append_log(self, ev: ConflictEvent) -> None:
		"""Append one JSON line per event to the file-based sink (best-effort —
		a log-write failure is logged to stderr but doesn't crash the daemon)."""
		if not self._log_path:
			return
		try:
			p = Path(self._log_path)
			p.parent.mkdir(parents=True, exist_ok=True)
			with p.open("a", encoding="utf-8") as fh:
				fh.write(
					json.dumps(
						{
							"kind": ev.kind,
							"private_ip": ev.private_ip,
							"origins": sorted(ev.origins),
							"at": ev.at,
						},
						sort_keys=True,
					)
					+ "\n"
				)
				fh.flush()
				os.fsync(fh.fileno())
		except Exception:
			# Best-effort. A conflict-log-write failure is operationally
			# interesting but not a reason to crash the mesh.
			pass


def observe_with_origins(
	tracker: ConflictTracker,
	table: OwnershipTable,
	latest_per_origin: dict,
) -> list[ConflictEvent]:
	"""The real public API: produce START/END events for `table`'s conflicts,
	using `latest_per_origin` to populate the `origins` set on each event."""
	now = tracker.now_fn()
	new = table.conflicts
	new_origins: dict[IP6, frozenset[HostID]] = {}
	for ip in new:
		origins = frozenset(origin for origin, adv in latest_per_origin.items() if ip in adv.owned)
		new_origins[ip] = origins
	started = sorted(new - tracker._prev)
	ended = sorted(tracker._prev - new)
	events: list[ConflictEvent] = []
	for ip in started:
		events.append(ConflictEvent("start", ip, new_origins[ip], now))
	for ip in ended:
		events.append(ConflictEvent("end", ip, tracker._prev_origins.get(ip, frozenset()), now))
	tracker._prev = new
	tracker._prev_origins = new_origins
	for ev in events:
		for cb in tracker._subscribers:
			cb(ev)
		tracker._append_log(ev)
	return events


__all__ = [
	"_DEFAULT_CONFLICTS_LOG",
	"ConflictEvent",
	"ConflictTracker",
	"observe_with_origins",
]
