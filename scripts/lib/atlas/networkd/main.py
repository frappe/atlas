"""`atlas-networkd` entry point (spec §9 + §14.4 + §16).

Stage 2: bring ``wg-mesh`` up peer-empty, install the bootstrap seed as
initial Membership Records, bind the ANCP UDP transport on the **public IPv6
endpoint** (not the wg-mesh /128), run the cold-join unicast
(``join.cold_join``) to every seed's public endpoint, apply the initial peer
table from the render pipeline, then enter the loop (scan + apply + gossip
fan-out + drain incoming + watchdog pat). On SIGTERM persist state + stop the
transport + sd_notify STOPPING.

ANCP rides plain UDP on public IPv6 addresses — the control plane is
independent of WireGuard (the *output* of the control plane, not its
transport).  This eliminates the bootstrap circular dependency entirely:
seeds' public endpoints and wg public keys are known from ``seed.json``, and
cold-join dials those endpoints directly without needing a wg-mesh peer entry
first.

The systemd unit ``scripts/systemd/atlas-networkd.service`` invokes this
module's ``main()`` under the Atlas venv, exactly like ``host-mesh.service``
invokes ``atlas.host_mesh.bring_up_mesh``.

The bootstrap contract (§8) is loaded eagerly: identity, keypair, and seed.
A missing identity or seed file fails loud (provisioning forgot to write it)
rather than coming up peer-empty with a fabricated identity (that would
pollute the cluster with a ghost).

This is the one module that touches the host system on the start path
(bring-up, periodic apply, UDP socket). Run with ``python3 -m
atlas.networkd.main`` for development; the ``sd_notify`` helpers short-circuit
when ``NOTIFY_SOCKET`` is absent so the daemon runs unchanged outside
systemd.
"""

from __future__ import annotations

import signal
import sys

from .._run import run as host_run
from . import commands, join, keys, sdnotify, seed
from .config import Config
from .config import load as load_config
from .daemon import Daemon, build_initial, default_envelope_verifier, default_signature_verifier
from .failure import FailureTracker
from .identity import load_identity
from .loop import Loop
from .observe import Counter
from .probe import ProbeProtocol
from .records import MembershipKind, MembershipRecord, MemberState
from .state import AppliedState, load_state, save_state
from .transport import UdpTransport


def main() -> int:
	"""Entry point — wired by `scripts/systemd/atlas-networkd.service`. Returns
	the exit code systemd reports. Every failure path raises loud
	(fail-at-boundary; Taste.md) so a misconfigured host fails its unit, not a
	silently-degraded mesh."""
	import time as _time

	from .conflicts import ConflictTracker as _CT

	config = load_config()
	identity = load_identity(config.identity_path)
	keys.ensure_keypair(
		config.private_key_path,
		public_key_path=config.public_key_path,
	)
	# Stage 5 — also ensure the ed25519 signing keypair (§19.3 defense in depth).
	# Generated fresh on first boot alongside the wg keypair; persisted at
	# /etc/atlas-networkd/signing-{private-key,public-key}.
	own_signing_priv, own_signing_pub = keys.ensure_signing_keypair(
		config.config_dir + "/signing-private-key",
		signing_pub_path=config.config_dir + "/signing-public-key",
	)
	public_key = _read_public_key(config.public_key_path)
	state = load_state(config.data_dir, seen_capacity=config.seen_cache_size)
	daemon = build_initial(
		identity,
		config,
		state,
		public_key,
		own_signing_priv_b64=own_signing_priv,
		own_signing_pub_b64=own_signing_pub,
	)
	_bring_up_mesh(daemon)
	seeds = _install_seed(daemon)
	transport = _start_transport(daemon)
	# Stage 4 — observer-local SWIM tracker + probe protocol.
	tracker = FailureTracker(now_fn=_time.monotonic)
	daemon.failure_tracker = tracker
	probe_protocol = ProbeProtocol(tracker=tracker, config=config, now_fn=_time.monotonic)
	daemon.probe_protocol = probe_protocol
	# Stage 5 — per-record verifier + metrics + conflict tracker.
	daemon.signature_verifier = default_signature_verifier
	daemon.metrics = Counter()
	daemon.conflict_tracker = _CT(now_fn=_time.monotonic)
	daemon.advertise_leaving = lambda: _advertise_leaving(daemon)
	# Stage 5+ — envelope verifier + seed-anchored trust directory (spec §19.1,
	# §19.4). The cache pre-populates from the seed's per-host
	# `signing_public_key` (§19.4); the operator pubkey is loaded from
	# `/etc/atlas-networkd/operator-public-key` (the §19.5 trust root). Both
	# are no-ops in tests that don't install the envelope verifier; production
	# wires both here so every incoming datagram is envelope-verified at the
	# boundary before any payload work.
	daemon.signing_pubkey_cache = seed.signing_pubkey_index(seeds)
	for _hid, _rec in state.membership.items():
		if _rec.signing_public_key and _hid not in daemon.signing_pubkey_cache:
			daemon.signing_pubkey_cache[_hid] = _rec.signing_public_key
	operator_pubkey = seed.load_operator_pubkey(config.config_dir + "/operator-public-key")
	daemon.operator_public_key = operator_pubkey
	daemon.envelope_verifier = default_envelope_verifier
	# Stage 5+ (§19.5) — load the operator-signed introduction certificate.
	# Empty for the initial-seed hosts (the seed anchors them — no introduction
	# needed); present for a host that joined an existing cluster
	# post-bootstrap (the controller signed its binding at provision time and
	# wrote it to /etc/atlas-networkd/introduction-signature). `cold_join`
	# attaches it to the first MembershipAdvertisement if non-empty.
	daemon.own_introduction_signature = _read_optional_file(config.config_dir + "/introduction-signature")
	sdnotify.ready()
	daemon.apply_if_changed()
	join.cold_join(daemon, transport, seeds)
	# §9.2 — pass the seeds to the Loop so `_cold_join_if_due` can re-send
	# the MembershipAdvertisement until a peer reply arrives. The Loop's
	# retry closes the one-shot failure mode: a dropped initial cold-join UDP
	# datagram would otherwise leave the newcomer peer-empty forever (gossip
	# doesn't carry the introduction_signature, so the existing hosts'
	# verifier would reject every subsequent TYPE_GOSSIP from the newcomer).
	# `join.cold_join` above is the first attempt (the original one-shot);
	# the loop owns attempts 2..N.
	loop = Loop(
		daemon=daemon,
		tick_interval=config.gossip_interval,
		probe_protocol=probe_protocol,
		cold_join_seeds=seeds,
	)
	_install_signal_handlers(loop, transport, daemon)
	loop.run()
	transport.stop()
	return 0


def _bring_up_mesh(daemon: Daemon) -> None:
	"""First-boot bring-up (spec §16.5): create the device, pin MTU, assign the
	host's own infra /128, bring it up, add the `fdaa::/16` route, then the
	atomic peer apply (which is empty on the first boot — anti-entropy fills it
	in Stage 2). Runs `sudo bash -c <bring_up_script>`, matching the predecessor
	`scripts/lib/atlas/host_mesh.py:bring_up_mesh` posture (sudo idempotent when
	the unit runs as root)."""
	daemon.run(
		"sudo bash -c {}",
		commands.bring_up_script(
			mesh_address=daemon.identity.mesh_address,
			mtu=daemon.config.wireguard_mtu,
			port=daemon.config.wg_host_port,
		),
	)


def _install_seed(daemon: Daemon) -> list:
	"""Install the bootstrap seed as initial Membership Records (spec §8 / §9.2).
	Each seed at its declared Generation goes into the applied-state via the
	standard apply rule (a higher-gen from a fresh seed replaces nothing; the
	apply is unconditional here because they're the first records for those
	origins). Persists state so a restart keeps the seeds. Returns the seed
	records so `main` can pass them to `cold_join` (the join dials these)."""
	seeds = seed.load_seed_optional(daemon.config.seed_path)
	for record in seeds:
		daemon.state.apply_membership(record)
	save_state(daemon.state, daemon.config.data_dir)
	return seeds


def _start_transport(daemon: Daemon) -> UdpTransport:
	"""Bind the ANCP UDP socket on the host's public endpoint, port
	``ancp_port`` (spec §13). Wires it onto the daemon so the loop's
	``_drain_incoming`` and ``_gossip_if_due`` find it. Raises on bind failure
	(port in-use, endpoint not assigned to a local interface) — fail loud at
	startup."""
	t = UdpTransport(bind=(daemon.identity.endpoint, daemon.config.ancp_port))
	t.start()
	daemon.transport = t
	return t


def _read_public_key(path: str) -> str:
	"""Read the public key the `keys.ensure_keypair` step just materialized. A
	missing file here means `ensure_keypair` silently no-op'd on a corrupted
	state — fail loud rather than advertise Membership Records carrying an empty
	pubkey."""
	from pathlib import Path

	return Path(path).read_text(encoding="utf-8").strip()


def _read_optional_file(path: str) -> str:
	"""Read a one-line file and return the stripped body, returning "" if the
	file is absent. Used for the §19.5 introduction-signature — present only on
	hosts that joined an existing cluster post-bootstrap; absent for initial-
	seed hosts (no introduction needed)."""
	from pathlib import Path

	p = Path(path)
	if not p.exists():
		return ""
	return p.read_text(encoding="utf-8").strip()


def _install_signal_handlers(loop: Loop, transport: UdpTransport, daemon: Daemon) -> None:
	"""SIGTERM → graceful shutdown (§14.4). SIGINT is the same (development).
	SIGHUP reloads config — Stage 5 (deferred; wiring it here would force config
	reload semantics we haven't specced yet)."""
	shutdown_started = []

	def _shutdown(*_args) -> None:
		if shutdown_started:
			return  # idempotent — a second SIGTERM doesn't double-anything
		shutdown_started.append(True)
		# §14.4 step 1: advertise `leaving` before the loop exits so peers
		# fast-path us to `dead` after `leaving_grace` (a short window —
		# the typical restart should refute before this matters). The hook
		# itself is wired in `main`; emit, then stop the loop.
		if daemon.advertise_leaving is not None:
			try:
				daemon.advertise_leaving()
			except Exception:
				pass  # best-effort — an exception at shutdown shouldn't abort
				# the persist path that follows in `daemon.shutdown()`.
		loop.running = False
		# The loop's `run` returns; `main` then calls `transport.stop()`. We
		# don't stop here because `transport.drain` may still read incoming
		# packets one last time inside the loop's final iteration; tearing the
		# socket down here would race that.

	signal.signal(signal.SIGTERM, _shutdown)
	signal.signal(signal.SIGINT, _shutdown)


def _advertise_leaving(daemon: Daemon) -> None:
	"""§14.4 — emit a Membership Record with `kind=leaving` at a fresh
	generation. The record propagates via gossip; peers fast-path alive→dead
	after `leaving_grace` once they apply this record."""
	daemon.state.bump_own_generation()
	own = MembershipRecord(
		host_id=daemon.identity.host_id,
		kind=MembershipKind.LEAVING,
		state=MemberState.LEAVING,
		endpoint=daemon.identity.endpoint,
		wg_public_key=daemon.own_membership.wg_public_key,
		mesh_address=daemon.identity.mesh_address,
		generation=daemon.state.own_generation,
		signing_public_key=daemon.own_signing_pub_b64,
	)
	daemon.state.apply_membership(own)
	save_state(daemon.state, daemon.config.data_dir)
	# Send immediately so the leave propagates before shutdown. We ride a
	# regular Gossip unicast to every alive peer (best-effort — peers who miss
	# this drop back to the `suspect_timeout` → `dead` ladder, which is the
	# safe default). Stage 5 may add a dedicated `leaving` fan-out for
	# determinism; Stage 4 reuses the gossip fan-out.
	from .gossip import GossipState, gossip_round

	gs = GossipState()
	gs.note_applied(own)
	if daemon.transport is not None:
		gossip_round(daemon, daemon.transport, gs)


if __name__ == "__main__":
	sys.exit(main())


__all__ = ["main"]
