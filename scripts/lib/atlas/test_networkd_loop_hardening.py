"""Tests for the M2 (public-UDP flood) + M8 (crash-loop) hardening of the
`atlas-networkd` main loop (spec §19 auth / §5 transport).

M2 — a flood of public-UDP datagrams must not monopolize the single-threaded
loop: the per-tick drain budget caps how many datagrams a tick verifies, and the
per-source rate limiter drops an abusive source BEFORE the ed25519 verify.

M8 — an exception from any subsystem step must NOT escape `run()`: `_guard`
logs + counts + continues; `KeyboardInterrupt`/`SystemExit` still propagate for
graceful shutdown.

Run with bare `python3 -m unittest` — no host, no kernel socket (a fake socket /
in-memory transport stands in).
"""

import unittest

from atlas.networkd.config import Config
from atlas.networkd.loop import Loop
from atlas.networkd.observe import Counter
from atlas.networkd.ratelimit import SourceRateLimiter
from atlas.networkd.transport import UdpTransport
from atlas.networkd.wire import Message


# --- SourceRateLimiter (M2 per-source gate) ----------------------------------


class TestSourceRateLimiter(unittest.TestCase):
	def test_allows_up_to_limit_then_drops_within_window(self):
		clock = [0.0]
		rl = SourceRateLimiter(limit=3, window=1.0, max_sources=100, now_fn=lambda: clock[0])
		# First 3 from one source pass, the 4th within the window is dropped.
		self.assertTrue(rl.allow("a"))
		self.assertTrue(rl.allow("a"))
		self.assertTrue(rl.allow("a"))
		self.assertFalse(rl.allow("a"))
		self.assertFalse(rl.allow("a"))

	def test_window_rollover_resets_the_count(self):
		clock = [0.0]
		rl = SourceRateLimiter(limit=2, window=1.0, max_sources=100, now_fn=lambda: clock[0])
		self.assertTrue(rl.allow("a"))
		self.assertTrue(rl.allow("a"))
		self.assertFalse(rl.allow("a"))
		clock[0] = 1.5  # window rolled over
		self.assertTrue(rl.allow("a"))

	def test_sources_are_independent(self):
		rl = SourceRateLimiter(limit=1, window=10.0, max_sources=100, now_fn=lambda: 0.0)
		self.assertTrue(rl.allow("a"))
		self.assertFalse(rl.allow("a"))  # a exhausted
		self.assertTrue(rl.allow("b"))  # b unaffected

	def test_limit_zero_disables_the_limiter(self):
		rl = SourceRateLimiter(limit=0, window=1.0, max_sources=100, now_fn=lambda: 0.0)
		for _ in range(1000):
			self.assertTrue(rl.allow("a"))

	def test_table_is_bounded_and_lru_evicted(self):
		# A spoofed-source flood must not grow the table past max_sources.
		rl = SourceRateLimiter(limit=5, window=100.0, max_sources=3, now_fn=lambda: 0.0)
		for i in range(50):
			rl.allow(f"src-{i}")
		self.assertLessEqual(len(rl._windows), 3)
		# The most recent source survives; the oldest was evicted.
		self.assertIn("src-49", rl._windows)
		self.assertNotIn("src-0", rl._windows)


# --- UdpTransport.drain budget + pre_filter (M2 transport seam) ---------------


class _FakeSocket:
	"""Returns queued (data, addr) pairs from recvfrom, then raises
	BlockingIOError like a non-blocking socket with an empty queue."""

	def __init__(self, datagrams):
		self._queue = list(datagrams)

	def recvfrom(self, _n):
		if not self._queue:
			raise BlockingIOError
		return self._queue.pop(0)


def _valid_datagram(sender="peer", addr=("2001:db8::9", 7946, 0, 0)):
	body = Message(type="ping", sender=sender, payload={}).to_bytes()
	return (body, addr)


class TestDrainBudget(unittest.TestCase):
	def test_budget_caps_dispatch_per_tick(self):
		t = UdpTransport(bind=("::1", 7946))
		t.socket = _FakeSocket([_valid_datagram() for _ in range(100)])
		seen = []
		dispatched = t.drain(lambda msg, addr: seen.append(msg), max_datagrams=10)
		self.assertEqual(dispatched, 10)
		self.assertEqual(len(seen), 10)

	def test_none_budget_drains_everything(self):
		t = UdpTransport(bind=("::1", 7946))
		t.socket = _FakeSocket([_valid_datagram() for _ in range(7)])
		seen = []
		dispatched = t.drain(lambda msg, addr: seen.append(msg), max_datagrams=None)
		self.assertEqual(dispatched, 7)

	def test_pre_filter_drops_before_dispatch(self):
		# pre_filter returning False for a source drops it; those are NOT
		# dispatched and NOT counted, but draining continues for others.
		t = UdpTransport(bind=("::1", 7946))
		good = ("2001:db8::good", 7946, 0, 0)
		bad = ("2001:db8::bad", 7946, 0, 0)
		t.socket = _FakeSocket(
			[
				_valid_datagram(addr=bad),
				_valid_datagram(addr=good),
				_valid_datagram(addr=bad),
				_valid_datagram(addr=good),
			]
		)
		seen = []
		dispatched = t.drain(
			lambda msg, addr: seen.append(addr),
			pre_filter=lambda addr: addr[0] != "2001:db8::bad",
		)
		self.assertEqual(dispatched, 2)  # only the two 'good' were dispatched
		self.assertTrue(all(a[0] == "2001:db8::good" for a in seen))


# --- Loop._drain_incoming integration (M2 budget + rate-limit-before-verify) --


class _CountingTransport:
	"""Feeds a fixed queue of (data, addr) through drain(), honoring the loop's
	max_datagrams + pre_filter exactly like UdpTransport."""

	def __init__(self, datagrams):
		self._all = list(datagrams)

	def drain(self, handler, max_datagrams=None, pre_filter=None):
		count = 0
		remaining = []
		for data, addr in self._all:
			if max_datagrams is not None and count >= max_datagrams:
				remaining.append((data, addr))
				continue
			if pre_filter is not None and not pre_filter(addr):
				continue
			from atlas.networkd.wire import from_bytes

			try:
				msg = from_bytes(data)
			except ValueError:
				continue
			count += 1
			handler(msg, addr)
		self._all = remaining
		return count


class _LoopDaemon:
	"""Minimal daemon-like stub for the loop's drain path: a transport, a
	metrics Counter, a config with the flood knobs, and a verify counter so a
	test can assert the rate limit gates BEFORE the ed25519 verify."""

	class _Identity:
		host_id = "h-self"

	def __init__(self, transport, config, verifier=None):
		self.transport = transport
		self.config = config
		self.metrics = Counter()
		self.identity = self._Identity()
		self.envelope_verifier = verifier
		self.verify_calls = 0

	def apply_stub(self):
		pass


def _loop_with(daemon):
	loop = Loop(daemon=daemon, tick_interval=0.001, now_fn=lambda: 0.0)
	return loop


class TestLoopDrainFloodDefense(unittest.TestCase):
	def _config(self, **kw):
		base = dict(
			inbound_tick_budget=5,
			inbound_rate_limit=1000,  # effectively off unless overridden
			inbound_rate_window=1.0,
			inbound_rate_max_sources=100,
		)
		base.update(kw)
		return Config().with_overrides(**base)

	def test_burst_over_budget_processes_at_most_budget_and_counts_exhaustion(self):
		# 50 datagrams from distinct sources (so the rate limit never bites),
		# budget=5 → at most 5 verified this tick + budget_exhausted counted.
		datagrams = [
			_valid_datagram(sender=f"peer{i}", addr=(f"2001:db8::{i}", 7946, 0, 0)) for i in range(50)
		]
		t = _CountingTransport(datagrams)
		verify_calls = [0]

		def verifier(msg, daemon):
			# Count the verify (the ed25519 cost) then RAISE so the datagram is
			# dropped after verify — isolates the drain/budget path from the full
			# handle_message dispatch (not under test here).
			verify_calls[0] += 1
			raise ValueError("drop after verify")

		daemon = _LoopDaemon(t, self._config(), verifier=verifier)
		loop = _loop_with(daemon)
		loop._drain_incoming()
		snap = daemon.metrics.snapshot()
		# At most budget verified this tick; the loop is free to run its other work.
		self.assertEqual(verify_calls[0], 5)
		self.assertEqual(snap.get("inbound_budget_exhausted"), 1)
		# The excess stayed in the buffer for the next tick (not dropped forever).
		self.assertEqual(len(t._all), 45)

	def test_rate_limited_source_dropped_before_verify(self):
		# One abusive source sends 20; rate_limit=3/window → 3 verified, 17
		# rate-limited BEFORE the verify (verify never sees them).
		bad = ("2001:db8::flood", 7946, 0, 0)
		datagrams = [_valid_datagram(sender="attacker", addr=bad) for _ in range(20)]
		t = _CountingTransport(datagrams)
		verify_calls = [0]

		def verifier(msg, daemon):
			verify_calls[0] += 1
			raise ValueError("drop after verify")  # isolate from handle_message

		cfg = self._config(inbound_tick_budget=1000, inbound_rate_limit=3)
		daemon = _LoopDaemon(t, cfg, verifier=verifier)
		loop = _loop_with(daemon)
		loop._drain_incoming()
		snap = daemon.metrics.snapshot()
		# Only 3 reached the ed25519 verify; the other 17 were dropped cheaply.
		self.assertEqual(verify_calls[0], 3)
		self.assertEqual(snap.get("inbound_rate_limited"), 17)

	def test_legitimate_low_rate_traffic_never_limited(self):
		# A handful of peers, a few datagrams each — well under the default
		# limits — must all pass (no rate-limit / budget drops).
		cfg = Config()  # spec defaults
		datagrams = []
		for p in range(4):  # 4 peers
			for _ in range(3):  # 3 datagrams each = 12 total
				datagrams.append(_valid_datagram(sender=f"peer{p}", addr=(f"2001:db8::{p}", 7946, 0, 0)))
		t = _CountingTransport(datagrams)

		def verifier(msg, daemon):
			raise ValueError("drop after verify")  # isolate from handle_message

		daemon = _LoopDaemon(t, cfg, verifier=verifier)
		loop = _loop_with(daemon)
		loop._drain_incoming()
		snap = daemon.metrics.snapshot()
		self.assertIsNone(snap.get("inbound_rate_limited"))
		self.assertIsNone(snap.get("inbound_budget_exhausted"))


# --- Loop._guard / run() exception containment (M8) ---------------------------


class _GuardDaemon:
	"""A daemon-like stub whose scan step raises on the first call. The loop's
	_guard must catch it, bump `loop_step_error`, and continue."""

	class _C:
		ownership_scan_interval = 0.0  # due every tick so the raising step reruns
		apply_debounce = 0.01
		anti_entropy_interval = 10.0
		probe_interval = 10.0
		gossip_interval = 10.0
		dead_grace = 5.0
		ownership_grace = 10.0
		inbound_tick_budget = 256
		inbound_rate_limit = 64
		inbound_rate_window = 1.0
		inbound_rate_max_sources = 100

	class _Identity:
		host_id = "h-self"

	class _State:
		def __init__(self):
			self.ownership = {"h-self": "ADV"}

	def __init__(self, boom):
		self.config = self._C()
		self.identity = self._Identity()
		self.state = self._State()
		self.transport = None
		self.probe_protocol = None
		self.failure_tracker = None
		self.metrics = Counter()
		self._boom = boom
		self.scan_calls = 0
		self.watchdog_calls = 0

	def scan_local_ownership(self):
		self.scan_calls += 1
		if self._boom is not None:
			raise self._boom
		return False

	def apply_if_changed(self):
		return False

	def notify_watchdog(self):
		self.watchdog_calls += 1
		# Stop the loop after a couple of ticks so run() returns in the test.
		return False


class TestLoopGuard(unittest.TestCase):
	def test_step_exception_does_not_escape_run_and_is_counted(self):
		daemon = _GuardDaemon(boom=RuntimeError("render blew up"))
		loop = Loop(daemon=daemon, tick_interval=0.0, now_fn=lambda: 0.0)
		ticks = [0]

		# Wrap watchdog to stop after 3 ticks so run() terminates.
		orig = daemon.notify_watchdog

		def stop_after_three():
			ticks[0] += 1
			if ticks[0] >= 3:
				loop.running = False
			return orig()

		daemon.notify_watchdog = stop_after_three
		# Patch sleep to a no-op so the test is fast.
		import atlas.networkd.loop as loopmod

		orig_sleep = loopmod.time.sleep
		loopmod.time.sleep = lambda _s: None
		try:
			loop.run()  # must return, NOT raise
		finally:
			loopmod.time.sleep = orig_sleep
		# The scan raised each tick but the loop kept going and counted it.
		self.assertGreaterEqual(daemon.scan_calls, 3)
		self.assertGreaterEqual(daemon.metrics.snapshot().get("loop_step_error", 0), 3)

	def test_keyboard_interrupt_propagates_for_shutdown(self):
		daemon = _GuardDaemon(boom=KeyboardInterrupt())
		loop = Loop(daemon=daemon, tick_interval=0.0, now_fn=lambda: 0.0)
		import atlas.networkd.loop as loopmod

		orig_sleep = loopmod.time.sleep
		loopmod.time.sleep = lambda _s: None
		try:
			with self.assertRaises(KeyboardInterrupt):
				loop.run()
		finally:
			loopmod.time.sleep = orig_sleep

	def test_system_exit_propagates_for_shutdown(self):
		daemon = _GuardDaemon(boom=SystemExit(1))
		loop = Loop(daemon=daemon, tick_interval=0.0, now_fn=lambda: 0.0)
		import atlas.networkd.loop as loopmod

		orig_sleep = loopmod.time.sleep
		loopmod.time.sleep = lambda _s: None
		try:
			with self.assertRaises(SystemExit):
				loop.run()
		finally:
			loopmod.time.sleep = orig_sleep


if __name__ == "__main__":
	unittest.main()
