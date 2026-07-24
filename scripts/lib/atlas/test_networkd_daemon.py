"""Unit tests for `networkd.keys`, `networkd.identity`, `networkd.sdnotify`,
`networkd.loop`, `networkd.daemon`. The host-touching seams (`wg genkey`,
`bash -c apply_script`, sd_notify's AF_UNIX socket) are injected/monkeypatched
so these run with bare `python3 -m unittest` — no host, no wg, no systemd.
"""

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atlas.networkd import keys, sdnotify
from atlas.networkd.config import Config
from atlas.networkd.daemon import Daemon, build_initial
from atlas.networkd.identity import HostIdentity, load_identity
from atlas.networkd.loop import Loop, ScheduledApply
from atlas.networkd.records import (
	MembershipKind,
	MembershipRecord,
	MemberState,
)
from atlas.networkd.state import AppliedState

# --- keys --------------------------------------------------------------------


class TestEnsureKeypair(unittest.TestCase):
	def test_generates_when_absent(self):
		with tempfile.TemporaryDirectory() as d:
			priv = Path(d) / "priv"
			pub = Path(d) / "pub"
			# Stub `wg genkey` + `wg pubkey` so the test runs without the binary.
			with mock.patch("atlas.networkd.keys._generate_keypair", return_value=("PRIV1234=", "PUB5678=")):
				private, public = keys.ensure_keypair(str(priv), str(pub))
			self.assertEqual(private, "PRIV1234=")
			self.assertEqual(public, "PUB5678=")
			self.assertEqual(priv.read_text().strip(), "PRIV1234=")
			self.assertEqual(pub.read_text().strip(), "PUB5678=")
			# Mode check: 0600 for private, 0644 for public.
			self.assertEqual(os.stat(priv).st_mode & 0o777, 0o600)
			self.assertEqual(os.stat(pub).st_mode & 0o777, 0o644)

	def test_idempotent_when_pair_valid(self):
		# When both files exist AND the public is the legit mate of the private,
		# `ensure_keypair` must NOT regenerate (regeneration would silently rotate
		# the host's identity + force a cluster-wide Membership update).
		with tempfile.TemporaryDirectory() as d:
			priv = Path(d) / "priv"
			pub = Path(d) / "pub"
			priv.write_text("SECRETKEY=")
			pub.write_text("MATEPUB=")
			with mock.patch("atlas.networkd.keys._existing_pair_valid", return_value=True):
				with mock.patch("atlas.networkd.keys._generate_keypair") as gen:
					private, public = keys.ensure_keypair(str(priv), str(pub))
					gen.assert_not_called()
			self.assertEqual(private, "SECRETKEY=")
			self.assertEqual(public, "MATEPUB=")

	def test_regenerates_when_pair_mismatched(self):
		# A tampered / half-written pair (e.g. an interrupted first-boot) is
		# detected by re-deriving the public from the private + comparing.
		# Regenerate rather than trust a mismatched pair.
		with tempfile.TemporaryDirectory() as d:
			priv = Path(d) / "priv"
			pub = Path(d) / "pub"
			priv.write_text("SECRETKEY=")
			pub.write_text("WRONGPUB=")
			with mock.patch("atlas.networkd.keys._existing_pair_valid", return_value=False):
				with mock.patch(
					"atlas.networkd.keys._generate_keypair", return_value=("NEWPRIV=", "NEWPUB=")
				):
					private, public = keys.ensure_keypair(str(priv), str(pub))
			self.assertEqual(private, "NEWPRIV=")
			self.assertEqual(public, "NEWPUB=")


# --- identity ----------------------------------------------------------------


class TestLoadIdentity(unittest.TestCase):
	def test_loads_valid_identity(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "identity.json"
			p.write_text(
				json.dumps(
					{
						"host_id": "abc-123",
						"endpoint": "2001:db9::7",
						"mesh_address": "fdaa:0:0:a1b2::1",
					}
				)
			)
			ident = load_identity(str(p))
			self.assertEqual(ident.host_id, "abc-123")
			self.assertEqual(ident.endpoint, "2001:db9::7")
			self.assertEqual(ident.mesh_address, "fdaa:0:0:a1b2::1")

	def test_missing_file_raises(self):
		# A fresh host whose provision forgot to write identity.json must not
		# silently come up with a fabricated identity (would pollute the cluster).
		with tempfile.TemporaryDirectory() as d:
			with self.assertRaises(FileNotFoundError):
				load_identity(str(Path(d) / "nope.json"))

	def test_missing_field_raises(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "identity.json"
			p.write_text(json.dumps({"host_id": "x"}))  # missing endpoint + mesh_address
			with self.assertRaises(ValueError):
				load_identity(str(p))


# --- sdnotify ----------------------------------------------------------------


class TestSdnotify(unittest.TestCase):
	def test_notify_returns_false_when_socket_unset(self):
		# Running outside systemd (NOTIFY_SOCKET absent) is the development path;
		# `notify` is a silent no-op so the daemon runs unchanged under `python -m`.
		with mock.patch.dict(os.environ, {}, clear=True):
			self.assertFalse(sdnotify.notify("READY=1"))
			self.assertFalse(sdnotify.ready())
			self.assertFalse(sdnotify.watchdog())
			self.assertFalse(sdnotify.stopping())

	def test_notify_sends_datagram_when_socket_set(self):
		# Bind an AF_UNIX datagram socket, point NOTIFY_SOCKET at it, send a
		# READY=1, verify the socket received it. This exercises the real
		# protocol path (NOT a stub) — the only thing we trust without a real
		# systemd is the in-process datagram exchange.
		server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
		fd, addr = tempfile.mkstemp()
		os.close(fd)
		os.unlink(addr)
		server.bind(addr)
		server.settimeout(1.0)
		try:
			with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": addr}, clear=True):
				self.assertTrue(sdnotify.ready())
			data, _ = server.recvfrom(1024)
			self.assertEqual(data, b"READY=1")
		finally:
			server.close()
			try:
				os.unlink(addr)
			except FileNotFoundError:
				pass


# --- loop.ScheduledApply -----------------------------------------------------


class TestScheduledApply(unittest.TestCase):
	def test_schedule_sets_deadline(self):
		s = ScheduledApply()
		s.schedule(now=100.0, debounce=0.2)
		self.assertEqual(s.due_at, 100.2)

	def test_consume_before_deadline_no_op(self):
		s = ScheduledApply()
		s.schedule(now=100.0, debounce=0.2)
		self.assertFalse(s.consume_if_due(100.1))
		self.assertIsNotNone(s.due_at)  # still pending

	def test_consume_at_or_after_deadline(self):
		s = ScheduledApply()
		s.schedule(now=100.0, debounce=0.2)
		self.assertTrue(s.consume_if_due(100.2))
		self.assertIsNone(s.due_at)  # cleared so a burst fires once

	def test_burst_within_window_collapses_to_one_apply(self):
		# A second change WITHIN the debounce window must NOT push the deadline
		# out — bounded latency from the FIRST change (spec §16.4).
		s = ScheduledApply()
		s.schedule(now=100.0, debounce=0.2)
		original_due = s.due_at
		s.schedule(now=100.1, debounce=0.2)
		self.assertEqual(s.due_at, original_due)


# --- loop.Loop ---------------------------------------------------------------


def _fake_daemon(*, now_fn=None, config=None, identity=None, state=None, run=None, write=None) -> Daemon:
	"""A Daemon wired for tests: every seam replaced with an in-memory stub so
	`Loop.run()` exercises only the scheduling logic, not the kernel."""
	cfg = config or Config().with_overrides(
		ownership_scan_interval=10.0,  # huge so the loop waits for the fake clock
		apply_debounce=0.01,
		gossip_interval=0.001,  # tight tick so the loop iterates fast in tests
	)
	ident = identity or HostIdentity(host_id="h-self", endpoint="2001:db9::self", mesh_address="fdaa:0:0::1")
	st = state or AppliedState()
	writes = []
	written_configs = []
	do_run = run or (lambda *a, **kw: "")
	do_write = write or (lambda body: (writes.append(body), written_configs.append(body))[1])
	daemon = Daemon(
		identity=ident,
		config=cfg,
		state=st,
		own_membership=MembershipRecord(
			host_id="h-self",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::self",
			wg_public_key="PUB",
			mesh_address="fdaa:0:0::1",
			generation=1,
		),
		last_local_set=frozenset(),
		run=do_run,
		write_run_config=do_write,
		notify_ready=lambda: False,  # No-op in tests
		notify_watchdog=lambda: False,
		notify_stopping=lambda: False,
	)
	return daemon


class _FakeDaemonForLoop:
	"""A non-slots stub the Loop tests drive directly. The Loop needs `config`,
	`scan_local_ownership`, `apply_if_changed`, `notify_watchdog`, plus (per
	Stage 2's gossip-aware scan path) `state.ownership[host_id]` and
	`identity.host_id`. We provide exactly those, with counters the tests
	assert on. `Daemon` is `slots=True` (no attribute rewrite, so
	mock.patch.object can't delattr on cleanup), so we stub rather than patch
	the real thing.
	"""

	class _C:  # tiny config carrier
		ownership_scan_interval = 10.0
		apply_debounce = 0.01
		anti_entropy_interval = 10.0  # Stage 3 — keep the loop from anti-entropy-ing every fake tick
		probe_interval = 10.0  # Stage 4 — keep the loop from probing every fake tick
		dead_grace = 5.0
		ownership_grace = 10.0

	class _Identity:
		host_id = "h-test"

	class _State:
		def __init__(self) -> None:
			self.ownership = {"h-test": "ADV-STUB"}  # any truthy value

	def __init__(self) -> None:
		self.config = self._C()
		self.identity = self._Identity()
		self.state = self._State()
		self.transport = None  # Stage 2/4 — no transport in fake loop
		self.probe_protocol = None  # Stage 4 — no probe_protocol in fake loop
		self.failure_tracker = None  # Stage 4 — no tracker in fake loop
		self.scan_calls = 0
		self.apply_calls = 0
		self.watchdog_calls = 0

	def scan_local_ownership(self) -> bool:
		self.scan_calls += 1
		# Return True on the first scan only — one change, then steady.
		return self.scan_calls == 1

	def apply_if_changed(self) -> bool:
		self.apply_calls += 1
		return True

	def notify_watchdog(self) -> bool:
		self.watchdog_calls += 1
		return False


class TestLoop(unittest.TestCase):
	def test_scan_changed_schedules_and_runs_apply(self):
		# Drive the loop's _scan_if_due + _apply_if_due directly with an
		# injected clock — assert the apply fired when the debounce elapsed.
		daemon = _FakeDaemonForLoop()
		clock = [0.0]
		loop = Loop(daemon=daemon, tick_interval=0.001, now_fn=lambda: clock[0])
		for _ in range(50):
			clock[0] += 0.005  # 5 ms per "tick"
			if loop.running:
				now = loop._now()
				loop._scan_if_due(now)
				loop._apply_if_due(now)
			if daemon.apply_calls:
				break
		self.assertEqual(daemon.scan_calls, 1)
		self.assertEqual(daemon.apply_calls, 1)

	def test_loop_exits_on_running_false(self):
		# Set `running=False` BEFORE entering run() — the while body never
		# executes; the loop terminates immediately. Models the SIGTERM path.
		daemon = _FakeDaemonForLoop()
		loop = Loop(daemon=daemon, tick_interval=0.001, now_fn=lambda: 0.0)
		loop.running = False
		loop.run()  # must return without iterating
		# (No assertion needed — reaching here means the loop returned.)


# --- daemon.build_initial ----------------------------------------------------


class TestBuildInitial(unittest.TestCase):
	def test_starts_at_generation_one_on_first_boot(self):
		# An empty persisted state (gen=0) → first Membership at gen=1, and the
		# own counter advances to 1 (a subsequent bump yields 2).
		with tempfile.TemporaryDirectory() as d:
			cfg = Config().with_overrides(data_dir=d)
			state = AppliedState()  # gen=0
			ident = HostIdentity(host_id="h1", endpoint="2001:db9::h1", mesh_address="fdaa:0:0:1::1")
			daemon = build_initial(ident, cfg, state, public_key="PUBKEY")
			self.assertEqual(daemon.own_membership.generation, 1)
			self.assertEqual(state.own_generation, 1)
			self.assertEqual(state.membership["h1"].wg_public_key, "PUBKEY")

	def test_bumps_to_persisted_plus_one_on_restart(self):
		# A warm restart (§14.5) loaded persisted state with gen=5 → first
		# Membership at gen=6 (the fast-refute shape).
		with tempfile.TemporaryDirectory() as d:
			cfg = Config().with_overrides(data_dir=d)
			state = AppliedState()
			state.own_generation = 5  # simulating a loaded persisted counter
			ident = HostIdentity(host_id="h1", endpoint="2001:db9::h1", mesh_address="fdaa:0:0:1::1")
			daemon = build_initial(ident, cfg, state, public_key="PUBKEY")
			self.assertEqual(daemon.own_membership.generation, 6)
			self.assertEqual(state.own_generation, 6)

	def test_persists_state(self):
		# After build_initial the state JSON has the bumped generation recorded;
		# a crash immediately after does not lose the counter (§14.4 / §14.5).
		with tempfile.TemporaryDirectory() as d:
			cfg = Config().with_overrides(data_dir=d)
			state = AppliedState()
			ident = HostIdentity(host_id="h1", endpoint="2001:db9::h1", mesh_address="fdaa:0:0:1::1")
			build_initial(ident, cfg, state, public_key="PUBKEY")
			from atlas.networkd.state import load_state

			reloaded = load_state(d)
			self.assertEqual(reloaded.own_generation, 1)


# --- daemon.Daemon.scan_local_ownership --------------------------------------


class TestDaemonScan(unittest.TestCase):
	def test_scan_unchanged_returns_false(self):
		daemon = _fake_daemon()
		daemon.last_local_set = frozenset({"fdaa::1"})
		# Same set in the cache file → no change, no generation bump.
		with tempfile.TemporaryDirectory() as d:
			lo_path = Path(d) / "lo.json"
			lo_path.write_text(json.dumps({"owned": ["fdaa::1"]}))
			daemon.config = daemon.config.with_overrides(local_ownership_path=str(lo_path))
			self.assertFalse(daemon.scan_local_ownership())
			self.assertEqual(daemon.state.own_generation, 0)

	def test_scan_changed_bumps_generation_and_updates_advertisement(self):
		daemon = _fake_daemon()
		daemon.last_local_set = frozenset({"fdaa::1"})
		with tempfile.TemporaryDirectory() as d:
			lo_path = Path(d) / "lo.json"
			lo_path.write_text(json.dumps({"owned": ["fdaa::1", "fdaa::2"]}))
			daemon.config = daemon.config.with_overrides(local_ownership_path=str(lo_path), data_dir=d)
			self.assertTrue(daemon.scan_local_ownership())
			self.assertEqual(daemon.state.own_generation, 1)
			# The new advertisement is stored under the host's own origin.
			adv = daemon.state.ownership[daemon.identity.host_id]
			self.assertEqual(adv.generation, 1)
			self.assertEqual(adv.owned, frozenset({"fdaa::1", "fdaa::2"}))

	def test_scan_changed_persists_generation_before_returning(self):
		# H5 — the bumped own_generation MUST be on disk before scan returns True
		# (the loop gossips the new advertisement in the same tick). A crash
		# after the wire send but before an unrelated save_state would otherwise
		# leave the on-disk generation stale, and a restart would reuse an
		# already-advertised generation for different content — peers reject it
		# as stale (§12.1). Assert load_state sees the bumped value.
		from atlas.networkd.state import load_state

		daemon = _fake_daemon()
		daemon.last_local_set = frozenset({"fdaa::1"})
		with tempfile.TemporaryDirectory() as d:
			lo_path = Path(d) / "lo.json"
			lo_path.write_text(json.dumps({"owned": ["fdaa::1", "fdaa::2"]}))
			daemon.config = daemon.config.with_overrides(local_ownership_path=str(lo_path), data_dir=d)
			self.assertTrue(daemon.scan_local_ownership())
			# Persisted before any gossip could send the new generation.
			self.assertEqual(load_state(d).own_generation, daemon.state.own_generation)
			self.assertEqual(load_state(d).own_generation, 1)


# --- daemon.Daemon.apply_if_changed -----------------------------------------


class TestDaemonApply(unittest.TestCase):
	def test_apply_no_op_when_render_matches(self):
		# No change → no run, no write.
		run_calls = []
		write_calls = []
		daemon = _fake_daemon(
			run=lambda *a, **kw: run_calls.append(a) or "",
			write=lambda body: write_calls.append(body),
		)
		# Prime the last_applied to render's current output so apply_if_changed
		# short-circuits.
		daemon.last_applied_config = daemon.render_current()
		self.assertFalse(daemon.apply_if_changed())
		self.assertEqual(run_calls, [])
		self.assertEqual(write_calls, [])

	def test_apply_runs_and_writes_when_changed(self):
		run_calls = []
		write_calls = []
		daemon = _fake_daemon(
			run=lambda *a, **kw: run_calls.append(a) or "",
			write=lambda body: write_calls.append(body),
		)
		# Prime last_applied with something DIFFERENT from render → apply fires.
		daemon.last_applied_config = "STALE\n"
		self.assertTrue(daemon.apply_if_changed())
		self.assertEqual(len(run_calls), 1)  # exactly one wg syncconf call
		self.assertEqual(len(write_calls), 1)
		# After apply, last_applied_config matches the just-rendered desired.
		self.assertEqual(daemon.last_applied_config, daemon.render_current())

	def test_apply_invokes_bash_c_with_apply_script(self):
		# The apply must run `bash -c <apply_script>` (process substitution needs
		# bash, not the host's `sh -c`), exactly like the predecessor.
		run_calls = []
		daemon = _fake_daemon(run=lambda *a, **kw: run_calls.append(a) or "")
		daemon.last_applied_config = "STALE\n"
		daemon.apply_if_changed()
		self.assertEqual(run_calls[0][0], "sudo bash -c {}")
		# The second positional is the inner script body (syncconf + set key).
		self.assertIn("syncconf", run_calls[0][1])
		self.assertIn("private-key", run_calls[0][1])


# --- daemon conflict observability (spec §7.3 / §18.2 — H3) ------------------


def _member(host_id, key, mesh, endpoint="2001:db9::7"):
	return MembershipRecord(
		host_id=host_id,
		kind=MembershipKind.MEMBER,
		state=MemberState.ALIVE,
		endpoint=endpoint,
		wg_public_key=key,
		mesh_address=mesh,
		generation=1,
	)


class TestDaemonConflictObservability(unittest.TestCase):
	"""H3: the §7.3/§18.2 "report loudly" promise. The apply path MUST drive the
	`ConflictTracker` (jsonl events + counters) and write `status.json` for BOTH
	an owned-/128 conflict and an H2 mesh_address collision."""

	def _daemon_with_tracker(self, tmp):
		from atlas.networkd.conflicts import ConflictTracker
		from atlas.networkd.observe import Counter, wire_conflict_metrics
		from atlas.networkd.records import owning_advertisement

		clock = [1000.0]
		daemon = _fake_daemon()
		daemon.config = daemon.config.with_overrides(status_path=str(Path(tmp) / "status.json"))
		tracker = ConflictTracker(now_fn=lambda: clock[0])
		tracker._log_path = str(Path(tmp) / "conflicts.jsonl")
		daemon.conflict_tracker = tracker
		daemon.metrics = Counter()
		wire_conflict_metrics(tracker, daemon.metrics)
		return daemon, tracker, clock, owning_advertisement

	def _read_status(self, daemon):
		return json.loads(Path(daemon.config.status_path).read_text())

	def _read_jsonl(self, tmp):
		p = Path(tmp) / "conflicts.jsonl"
		if not p.exists():
			return []
		return [json.loads(line) for line in p.read_text().splitlines()]

	def test_owned_conflict_start_and_end_surface(self):
		with tempfile.TemporaryDirectory() as tmp:
			daemon, tracker, clock, adv = self._daemon_with_tracker(tmp)
			# Two origins both own fdaa::9 → owned §7.3 conflict.
			daemon.state.membership["h1"] = _member("h1", "K1", "fdaa:0:0:1::1")
			daemon.state.membership["h2"] = _member("h2", "K2", "fdaa:0:0:2::1")
			daemon.state.ownership["h1"] = adv("h1", 1, ("fdaa::9",))
			daemon.state.ownership["h2"] = adv("h2", 1, ("fdaa::9",))
			daemon.last_applied_config = "STALE\n"
			daemon.apply_if_changed()
			# jsonl: one START with {private_ip, origins}.
			lines = self._read_jsonl(tmp)
			starts = [l for l in lines if l["kind"] == "start"]
			self.assertEqual(len(starts), 1)
			self.assertEqual(starts[0]["private_ip"], "fdaa::9")
			self.assertEqual(sorted(starts[0]["origins"]), ["h1", "h2"])
			# status.json: active conflict + incremented counter.
			status = self._read_status(daemon)
			self.assertEqual(status["conflict_count"], 1)
			self.assertEqual(status["conflicts"][0]["private_ip"], "fdaa::9")
			self.assertEqual(sorted(status["conflicts"][0]["origins"]), ["h1", "h2"])
			self.assertEqual(status["metrics"]["conflict_started"], 1)
			self.assertEqual(status["metrics"]["conflicts_total"], 1)
			# h2 withdraws → conflict clears → END event, count back to 0.
			clock[0] = 2000.0
			daemon.state.ownership["h2"] = adv("h2", 2, ())
			daemon.last_applied_config = "STALE-AGAIN\n"
			daemon.apply_if_changed()
			ends = [l for l in self._read_jsonl(tmp) if l["kind"] == "end"]
			self.assertEqual(len(ends), 1)
			self.assertEqual(ends[0]["private_ip"], "fdaa::9")
			status2 = self._read_status(daemon)
			self.assertEqual(status2["conflict_count"], 0)
			self.assertEqual(status2["metrics"]["conflict_ended"], 1)

	def test_mesh_address_collision_surfaces(self):
		# H2 mesh_address collision: two members share a mesh_address /128 — NOT an
		# owned conflict (empty ownership table), so this proves the render-level
		# source is threaded out and surfaced too.
		with tempfile.TemporaryDirectory() as tmp:
			daemon, tracker, clock, _adv = self._daemon_with_tracker(tmp)
			daemon.state.membership["h1"] = _member("h1", "K1", "fdaa:0:0:5::1")
			daemon.state.membership["h2"] = _member("h2", "K2", "fdaa:0:0:5::1")  # same mesh
			daemon.last_applied_config = "STALE\n"
			daemon.apply_if_changed()
			starts = [l for l in self._read_jsonl(tmp) if l["kind"] == "start"]
			self.assertEqual(len(starts), 1)
			self.assertEqual(starts[0]["private_ip"], "fdaa:0:0:5::1")
			self.assertEqual(sorted(starts[0]["origins"]), ["h1", "h2"])
			status = self._read_status(daemon)
			self.assertEqual(status["conflict_count"], 1)
			self.assertEqual(status["metrics"]["conflict_started"], 1)

	def test_status_write_failure_does_not_crash_apply(self):
		# A status_path that can't be written (parent is a file) → best-effort:
		# the apply still runs, a counter is bumped, no exception escapes.
		with tempfile.TemporaryDirectory() as tmp:
			daemon, tracker, clock, adv = self._daemon_with_tracker(tmp)
			blocker = Path(tmp) / "afile"
			blocker.write_text("x")
			daemon.config = daemon.config.with_overrides(status_path=str(blocker / "status.json"))
			daemon.state.ownership["h1"] = adv("h1", 1, ("fdaa::9",))
			daemon.state.ownership["h2"] = adv("h2", 1, ("fdaa::9",))
			daemon.state.membership["h1"] = _member("h1", "K1", "fdaa:0:0:1::1")
			daemon.state.membership["h2"] = _member("h2", "K2", "fdaa:0:0:2::1")
			daemon.last_applied_config = "STALE\n"
			# Must not raise.
			self.assertTrue(daemon.apply_if_changed())
			self.assertEqual(daemon.metrics.snapshot().get("status_write_failed"), 1)

	def test_conflict_observed_even_when_config_bytes_unchanged(self):
		# A conflict that clears can render byte-identical to a prior state; the
		# observe path must run BEFORE the drift short-circuit so the END still
		# fires + status.json refreshes.
		with tempfile.TemporaryDirectory() as tmp:
			daemon, tracker, clock, adv = self._daemon_with_tracker(tmp)
			daemon.state.ownership["h1"] = adv("h1", 1, ("fdaa::9",))
			daemon.state.ownership["h2"] = adv("h2", 1, ("fdaa::9",))
			daemon.state.membership["h1"] = _member("h1", "K1", "fdaa:0:0:1::1")
			daemon.state.membership["h2"] = _member("h2", "K2", "fdaa:0:0:2::1")
			daemon.apply_if_changed()  # START (config also changed from "")
			self.assertEqual(self._read_status(daemon)["conflict_count"], 1)
			# Now clear the conflict; prime last_applied to the NEW render so the
			# drift check would short-circuit — the observe path must still run.
			daemon.state.ownership["h2"] = adv("h2", 2, ())
			clock[0] = 3000.0
			daemon.last_applied_config = daemon.render_current()
			self.assertFalse(daemon.apply_if_changed())  # no drift → no syncconf
			self.assertEqual(self._read_status(daemon)["conflict_count"], 0)
			self.assertEqual(daemon.metrics.snapshot()["conflict_ended"], 1)


if __name__ == "__main__":
	unittest.main()
