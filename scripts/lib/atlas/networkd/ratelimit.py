"""Per-source inbound rate limiter (spec §19 — flood defense).

ANCP rides plaintext public UDP on port 7946 (§5, §19), reachable from the IPv6
internet. Every inbound datagram otherwise triggers an ed25519 envelope verify
(~50-100 µs) on the single-threaded loop BEFORE any cheap filter — so a remote
flood forces one verify per packet and starves gossip/probe/apply (a targeted
blackhole: peers push the flooded host toward suspect/dead).

`SourceRateLimiter` is a cheap fixed-window counter keyed by remote IP, checked
BEFORE the crypto verify: an abusive source is dropped without the ed25519 cost.
The window is fixed (not a sliding log) so each source costs one int + one float,
and the table is capped + LRU-evicted so the limiter itself can never be a
memory-exhaustion vector (an attacker spraying spoofed source IPs can't grow it
past `max_sources`). Pure: no I/O, an injected `now_fn` for tests.

Defaults (config.py) are generous vs. legitimate traffic: a handful of peers at
200 ms / 1 s cadences emit a few datagrams/sec/source — far under the limit — so
honest gossip/probe/anti-entropy is never dropped.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(slots=True)
class SourceRateLimiter:
	"""Fixed-window token counter keyed by source IP. `allow(ip)` returns True if
	the source is under `limit` datagrams in the current `window` seconds, False
	(drop) otherwise. Bounded to `max_sources` entries, LRU-evicted on insert so
	a spoofed-source flood can't exhaust memory."""

	limit: int
	window: float
	max_sources: int
	now_fn: Callable[[], float] = field(default=time.monotonic)
	# ip -> [window_start, count]; OrderedDict for O(1) LRU move/pop.
	_windows: "OrderedDict[str, list[float]]" = field(default_factory=OrderedDict)

	def allow(self, ip: str) -> bool:
		"""True iff `ip` may be processed this call; False → drop cheaply (before
		any parse/verify). A non-positive `limit` disables the limiter (always
		allow) so an operator can turn it off via config."""
		if self.limit <= 0:
			return True
		now = self.now_fn()
		entry = self._windows.get(ip)
		if entry is None or now - entry[0] >= self.window:
			# Fresh source or the window rolled over — start a new window at 1.
			self._windows[ip] = [now, 1]
			self._windows.move_to_end(ip)
			self._evict_if_full()
			return True
		# Same window — bump and gate. Touch LRU so an active (allowed) source
		# isn't evicted out from under itself.
		self._windows.move_to_end(ip)
		if entry[1] >= self.limit:
			return False
		entry[1] += 1
		return True

	def _evict_if_full(self) -> None:
		"""Pop least-recently-used sources until the table is within cap. Runs
		after an insert so the newest source stays (it was just moved to end)."""
		while len(self._windows) > self.max_sources:
			self._windows.popitem(last=False)


__all__ = ["SourceRateLimiter"]
