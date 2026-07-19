"""The daemon orchestrator (spec §6 + §16.4). Composes the pure pieces into
host-touching steps: scan local ownership, recompute WgDesired, atomic apply.
Kept small and split into ~10-line methods so each is one operation; the loop
(`loop.py`) drives them on the right timers (spec §11.2 / §16.4).

Dependency injection: the host-touching seams (`run`, `write_run_config`,
`sd_notify` helpers) are overridable per-instance so the apply path is
unit-testable without touching the kernel — a test passes a `run` that records
argv instead of `subprocess.run`, the same shape `scripts/lib/atlas/test_*`
already use through `_run`/`_run_input`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .._run import run as host_run
from . import commands, render, sdnotify
from .config import Config
from .identity import HostIdentity
from .localownership import read_local_ownership, same_set
from .records import (
	MembershipKind,
	MembershipRecord,
	MemberState,
	effective_ownership,
	owning_advertisement,
)
from .signing import SignatureError, sign, verify
from .state import AppliedState, save_state

# `run(command_template, *params, check=True, quiet=False) -> stdout`. Host's
# `_run.run` raises CommandError on non-zero (converging apply, §16.4). Tests
# pass a stub that records argv or raises — the same call shape either way.
RunFn = Callable[..., str]


def _default_write_run_config(body: str) -> None:
	"""The hot-path writer: `/run/atlas-networkd/wg-mesh.conf`. `/run` is tmpfs
	so it never persists across reboot (the persisted state is the JSON under
	`/var/lib/atlas-networkd/`). Atomic via tempfile + `os.replace` so a crash
	mid-write doesn't leave a half-config for `wg syncconf` to choke on."""
	p = Path(commands.WG_CONFIG_PATH)
	p.parent.mkdir(parents=True, exist_ok=True)
	tmp = p.with_suffix(p.suffix + ".tmp")
	tmp.write_text(body, encoding="utf-8")
	os.replace(tmp, p)


@dataclass(slots=True)
class Daemon:
	"""One instance per host. Holds the frozen identity, the applied-state
	(persisted), the last scanned /128 set, and the last applied config bytes.
	The loop in `loop.py` is pure scheduling on top of these — each method does
	one operation."""

	identity: HostIdentity
	config: Config
	state: AppliedState
	own_membership: MembershipRecord
	last_local_set: frozenset[str] = field(default_factory=frozenset)
	last_applied_config: str = ""
	# The ANCP UDP transport (`transport.py`); the daemon owns the socket, the
	# loop polls it each tick. `Optional` so `build_initial` can construct a
	# Daemon without yet binding the socket — `main.py` wires `.transport =
	# UdpTransport(...).start()` after `build_initial`. Tests inject an
	# in-memory queue pair (`FakeTransport` in `test_networkd_gossip`) so the
	# gossip round is end-to-end testable without a kernel.
	transport: object | None = field(default=None, init=True)
	# The SWIM probe protocol (Stage 4) — wired by `main.py` after the
	# transport; None during early bootstrap + in tests that don't drive
	# probes. The gossip `handle_message` path defends against None.
	probe_protocol: object | None = field(default=None, init=True)
	# The observer-local failure tracker (Stage 4) — owned by the daemon so
	# gossip's refute-trigger (`note_alive`) and the probe protocol share it.
	# Tests inject a `FailureTracker` with a controlled `now_fn`.
	failure_tracker: object | None = field(default=None, init=True)
	# Stage 5 — the conflict event tracker (§7.3 / §18.2). The loop's
	# `_gc_if_due` and the apply step observe the effective table's conflicts
	# via this; START/END events fire the operator hook + the metrics counter.
	conflict_tracker: object | None = field(default=None, init=True)
	# Stage 5 — ed25519 signature verifier hook (§19.3). Set by `main.py` to
	# the default verifier (`signing.verify` over the canonical record dict +
	# the origin's published signing pubkey); None means "no signature
	# verification" (the in-test path; production always sets it).
	signature_verifier: Callable[[object, object], None] | None = field(default=None, init=True)
	# Stage 5 — metrics counter (§20.2). `gossip._apply_record` incr's
	# `signature_failed` on a verify failure.
	metrics: object | None = field(default=None, init=True)
	# Stage 5 — wire-signature side-channel: a dict keyed by `id(record)`
	# carrying the incoming record's wire bytes signature, populated by the
	# gossip / anti-entropy apply path before invoking the verifier. The
	# frozen slots dataclasses can't carry ad-hoc attributes, so the sig
	# lives here keyed by record identity. Cleared each apply round.
	_incoming_wire_sigs: dict | None = field(default=None, init=True)
	# Stage 5 — the daemon's own signing key (base64 ed25519 private). Used by
	# `scan_local_ownership`, `build_initial`, and `_advertise_leaving` to
	# sign outgoing records. "" means "don't sign" (pre-Stage-5 path; tests).
	own_signing_priv_b64: str = field(default="", init=True)
	# Injected seams (production defaults wired here; tests override).
	run: RunFn = field(default=host_run)
	write_run_config: Callable[[str], None] = field(default=_default_write_run_config)
	notify_ready: Callable[[], bool] = field(default=sdnotify.ready)
	notify_watchdog: Callable[[], bool] = field(default=sdnotify.watchdog)
	notify_stopping: Callable[[], bool] = field(default=sdnotify.stopping)
	# A hook for the §14.4 graceful-shutdown `leaving` advertisement.
	# main.py wires it; tests can swap it. None means "do nothing" for tests
	# that don't care about shutdown.
	advertise_leaving: Callable[[], None] | None = field(default=None, init=True)

	# --- unicast send (the reply path for §9.1 bundle reply) ----------------

	def unicast_send(self, endpoint: str, data: bytes) -> None:
		"""Send a single ANCP datagram to ``(endpoint, ancp_port)``. Used by
		gossip/probe/anti-entropy handlers to reply to a peer's request. A
		no-op if ``daemon.transport`` isn't wired yet — a pre-loop caller gets
		the silent no-op so bootstrap doesn't crash before
		``transport.start()`` runs."""
		t = self.transport
		if t is None:
			return  # tests / pre-loop bootstrap can hit this; fail-soft, the
			# next gossip round will spread the record via the normal fan-out.
		t.send((endpoint, self.config.ancp_port), data)

	# --- scan (spec §11) ----------------------------------------------------

	def scan_local_ownership(self) -> bool:
		"""Read the /etc/atlas-networkd/local-ownership.json cache (§11.1) and on
		a changed set, bump the host's own Ownership Generation (§12.1) and
		update the applied-state's advertisement for this origin. Returns True
		iff the set changed this scan (the loop uses the bool to schedule the
		debounced apply). Stage 5: the signature is attached at wire-serialize
		time (in `gossip` / `antientropy`), not here — keeps the apply-state's
		record byte-equivalent to the pure-data record."""
		scanned = read_local_ownership(self.config.local_ownership_path)
		if same_set(scanned, self.last_local_set):
			return False
		self.last_local_set = scanned
		self.state.bump_own_generation()
		adv = owning_advertisement(
			origin=self.identity.host_id,
			generation=self.state.own_generation,
			owned=scanned,
		)
		self.state.apply_ownership(adv)
		return True

	# --- apply (spec §16) ----------------------------------------------------

	def render_current(self) -> str:
		"""Recompute the canonical wg-mesh config body from the persisted state:
		effective Ownership = union of latest per-origin advertisements;
		membership = the table the apply pipeline reads. Renders the host's own
		host_id out (a host never peers with itself)."""
		ownership = effective_ownership(self.state.ownership)
		return render.render_wg_desired(self.identity.host_id, self.state.membership, ownership)

	def apply_if_changed(self) -> bool:
		"""Render, drift-check, push on drift (spec §16.4 — atomic whole-table
		`wg syncconf`). Returns True iff an apply ran. The §16.3 non-overlap
		invariant is asserted by the render itself. The apply path runs
		`sudo bash -c <apply_script>` exactly like the predecessor
		`host_mesh._push_wg_mesh` — `bash -c` for the process substitution;
		`sudo` no-op when the unit already runs as root, matching the existing
		lib. Raises on non-zero (converging apply — a failed syncconf is a
		partition, not a soft warning)."""
		desired = self.render_current()
		if desired == self.last_applied_config:
			return False
		self.write_run_config(desired)
		self.run("sudo bash -c {}", commands.apply_script())
		self.last_applied_config = desired
		return True

	# --- shutdown (spec §14.4) ----------------------------------------------

	def shutdown(self) -> None:
		"""Persistence + sd_notify STOPPING. Called from the SIGTERM handler
		before the loop exits (graceful shutdown, §14.4). The leaving Membership
		Advertisement (spec §14.4 step 1) is a Stage-5 addition — Stage 1b just
		persists state so a restart recovers the Generation counter."""
		save_state(self.state, self.config.data_dir)
		self.notify_stopping()


def build_initial(
	identity: HostIdentity,
	config: Config,
	state: AppliedState,
	public_key: str,
	own_signing_priv_b64: str = "",
	own_signing_pub_b64: str = "",
) -> Daemon:
	"""Construct the host's first Membership Record for itself (§9). Called at
	daemon startup after `keys.ensure_keypair` + `keys.ensure_signing_keypair`
	so the wg pubkey + ed25519 signing pubkey exist to be advertised.
	Generation = `state.own_generation + 1` — a restart bumps to `persisted+1`
	(§14.5 fast-refute shape); a first boot starts at 1.

	Stage 5: the Membership Record carries `signing_public_key` (the ed25519
	pubkey peers will use to verify subsequent records from this origin).
	Outbound signatures are attached at wire-serialize time (in `gossip` /
	`join` / `antientropy`), not at record-store time — keeps the dataclass
	immutable and the apply-state shape byte-equivalent across stages."""
	current_gen = state.own_generation + 1
	own = MembershipRecord(
		host_id=identity.host_id,
		kind=MembershipKind.MEMBER,
		state=MemberState.ALIVE,
		endpoint=identity.endpoint,
		wg_public_key=public_key,
		mesh_address=identity.mesh_address,
		generation=current_gen,
		signing_public_key=own_signing_pub_b64,
	)
	state.bump_own_generation()  # consume the +1 we used
	state.apply_membership(own)  # the apply rule replaces wholesale on higher gen
	save_state(state, config.data_dir)  # persist so a crash keeps gen ≥ current
	return Daemon(
		identity=identity,
		config=config,
		state=state,
		own_membership=own,
		own_signing_priv_b64=own_signing_priv_b64,
	)


__all__ = ["Daemon", "build_initial", "default_signature_verifier"]


def default_signature_verifier(record, daemon) -> None:
	"""The production verifier (§19.3) wired by `main.py`. For a Membership
	Record: verify the wire-dict signature against the record's own
	`signing_public_key`. For an Ownership Advertisement: verify against the
	origin's cached signing pubkey (looked up from the applied Membership
	Record for that origin). Raises `SignatureError` on any failure.

	Records WITH `signing_public_key` set MUST carry a valid wire signature —
	unsigned records from a peer that advertises a signing key are rejected.
	Records WITHOUT `signing_public_key` are accepted unsigned (pre-Stage-5
	peer path; transport-binding trust is the only defense).

	The actual wire signature is threaded in via the daemon's
	`_incoming_wire_sigs` side-channel keyed by the record object's `id()`;
	the gossip / anti-entropy apply path populates it before invoking the
	verifier.
	"""
	from . import wire
	from .records import MembershipRecord, OwnershipAdvertisement

	sigs = getattr(daemon, "_incoming_wire_sigs", None) or {}
	wire_sig = sigs.get(id(record))
	if isinstance(record, MembershipRecord):
		existing = daemon.state.membership.get(record.host_id)
		if not record.signing_public_key:
			if existing is not None and existing.signing_public_key:
				raise SignatureError(
					f"MembershipRecord from {record.host_id} drops signing_public_key "
					"(downgrade attempt rejected)"
				)
			return  # pre-Stage-5 peer — accept unsigned
		if not wire_sig:
			raise SignatureError(
				f"MembershipRecord from {record.host_id} has signing_public_key but carries no wire signature"
			)
		d = wire.membership_to_dict(record)
		d["signature"] = wire_sig
		# Verify against the EXISTING stored signing key when available (§19.3
		# key-rotation binding). Without this, any relay can hijack an origin's
		# signing key by publishing a MembershipRecord with a fresh keypair —
		# the verifier would accept it against the record's own key, not the
		# origin's established key.
		if existing is not None and existing.signing_public_key:
			verify(d, existing.signing_public_key, kind="membership")
		else:
			verify(d, record.signing_public_key, kind="membership")
		return
	if isinstance(record, OwnershipAdvertisement):
		origin_membership = daemon.state.membership.get(record.origin)
		if origin_membership is None or not origin_membership.signing_public_key:
			return
		if not wire_sig:
			raise SignatureError(
				f"OwnershipAdvertisement from {record.origin} has signing_public_key "
				"but carries no wire signature"
			)
		d = wire.ownership_to_dict(record)
		d["signature"] = wire_sig
		verify(d, origin_membership.signing_public_key, kind="ownership")
		return
