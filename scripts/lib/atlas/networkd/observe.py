"""Metrics + structured event log helper (spec §18.2 / §20.2 — the observability
surface).

The daemon exposes a small in-memory counter surface keyed by event name; the
loop wires `daemon.metrics = Counter()` and the various stages incr on the
relevant transitions:

  - `signature_failed` — a record's ed25519 signature failed verify (§19.3).
  - `conflict_started` / `conflict_ended` — a /128 entered/left the conflicting
    state (§7.3 / §18.2).
  - `peer_suspect` / `peer_dead` / `peer_refuted` — observer-local ladder
    transitions (Stage 4).
  - `apply_count` — the §16.4 atomic `wg syncconf` ran.
  - `gossip_message_recv` — a UDP datagram dispatched (Stage 2).
  - `anti_entropy_pull` — the §15 daemon pulled a peer's summary (Stage 3).

The counter is exposed via `daemon.metrics.snapshot()` as a plain dict so a
future HTTP `/metrics` endpoint (Stage 6 / observability follow-up) or a
`journalctl`-style debug print has one place to read. Pure: process-local, no
I/O. Persisted to disk by a separate `observe.snapshot_writer` if the operator
wants a periodic JSON dump — Stage 5 leaves the operator hook in place; the
dump cadence is a follow-up.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(slots=True)
class Counter:
	"""A simple in-memory named counter. Thread-safe-ish: the daemon's loop is
	single-threaded so no lock is needed; `incr` is just `dict[ name ] += 1`.
	Snapshots are read-only and cheap."""

	_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

	def incr(self, name: str, by: int = 1) -> None:
		"""Increment a named counter. A new name starts from zero + the by."""
		self._counts[name] += by

	def snapshot(self) -> dict[str, int]:
		"""Return the current counts as a plain dict (copy — callers may keep
		the result without worrying about later mutating it)."""
		return dict(self._counts)


def wire_conflict_metrics(conflict_tracker, counter: Counter) -> None:
	"""Subscribe `counter` to `conflict_tracker` so a conflict START bumps
	`conflict_started` + `conflicts_total`, and a conflict END bumps
	`conflict_ended` (spec §7.3 / §18.2). This is the clean integration seam the
	tracker's `subscribe` docstring refers to — `main.py` calls it once at
	startup; the apply path's `observe_conflicts` then drives the events. These
	counters ride in `status.json` alongside the active conflict list."""

	def _on_event(ev) -> None:
		if ev.kind == "start":
			counter.incr("conflict_started")
			counter.incr("conflicts_total")
		elif ev.kind == "end":
			counter.incr("conflict_ended")

	conflict_tracker.subscribe(_on_event)


def wire_default_metrics(daemon) -> Counter:
	"""Attach a `Counter` to `daemon.metrics` and wire the simple stage-5
	hooks: incr `signature_failed` (already done in gossip `_apply_record`;
	here we just attach the counter so the incr has somewhere to land),
	incr `conflict_started/end` via the daemon's `ConflictTracker`, incr
	`peer_suspect/dead/refuted` via the daemon's `FailureTracker`. Returns the
	counter for the caller to wire further.

	The actual wiring of stage-specific transitions is in the relevant stages'
	code (gossip for signature_failed, conflict_tracker.subscribe for conflicts,
	FailureTracker for peer-suspect); this function just ensures `daemon.metrics`
	exists."""
	counter = Counter()
	daemon.metrics = counter  # type: ignore[attr-defined]
	return counter


__all__ = ["Counter", "wire_conflict_metrics", "wire_default_metrics"]
