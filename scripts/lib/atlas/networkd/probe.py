"""SWIM probe protocol (spec §14.2) — the failure-detection mechanism that
drives the `alive → suspect → dead` ladder in `failure.py`.

Every `probe_interval` (default 1 s), the daemon picks `probe_peers` (default
3) random members, sends each a direct `ping`. Acks received within
`probe_timeout` (default 500 ms) keep the peer `alive`. Acks received via K
indirect relays (default 3) within `indirect_timeout` (default 2 s) also keep
the peer `alive` (SWIM's indirect ping — a one-way partition between prober
and target doesn't false-evict if the prober can reach the target through a
	relay). No ack within `indirect_timeout` → `mark_suspect(target)` and the
suspect window opens (driven by `FailureTracker.gc` ticks that promote to
`dead` after `suspect_timeout`).

Refute (§14.2 paragraph 4): the target clears the suspicion by sending an
`alive` Membership Record at a higher Generation than the suspicing observer
	stored; the §10.3 monotonic apply rule accepts it, and a top-level trigger
	(from `gossip.handle_message` after applying any Membership update) calls
	`FailureTracker.note_alive(host_id)` to reset the ladder. Stage 4 wires
	the Trigger; Stage 5 formalizes the explicit fast-refute wire type if we
	need it for latency.

The wire types `TYPE_PING / TYPE_ACK / TYPE_INDIRECT_PING` are declared in
	`wire.py`; the payload helpers here are pure. The probe protocol is the
	only consumer of those types so the dispatch in `gossip.handle_message`
	routes them to `handle_ping` / `handle_ack` / `handle_indirect_ping` here.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from . import wire
from .config import Config
from .failure import FailureTracker
from .peers import select_peers
from .transport import UdpTransport
from .wire import (
	TYPE_ACK,
	TYPE_INDIRECT_PING,
	TYPE_PING,
	Message,
)

# The probe pace — one round every `probe_interval` (default 1 s). A `tick` here
# is the body of `probe_round`; the loop calls it on the `probe_interval`
# cadence. Wire types + handlers are pure; the round itself composes them.


@dataclass(slots=True)
class ProbeProtocol:
	"""The prober-side state: in-flight pings awaiting acks (keyed by nonce),
	the injected clock, and a small bookkeeping of "due to mark suspect" if
	no ack arrives in time. The ack matching + suspect-marking logic is here;
	the ladder is `FailureTracker`'s.

	Kept tiny: in-flight pings is bounded by `probe_peers` (one nonce per
	direct target per round) plus the odd indirect forwarded ping. Memory is
	O(probe_peers + recent_forwarded) per prober.
	"""

	tracker: FailureTracker
	config: Config
	# `now_fn` injected — `time.monotonic` in production, controlled in tests.
	now_fn: Callable[[], float] = field(default=time.monotonic)
	# nonce -> (target_host_id, deadline). Acks are matched by nonce; the
	# deadline is when, if no ack has arrived, we mark suspect and stop waiting.
	in_flight: dict[int, tuple[str, float]] = field(default_factory=dict)
	# nonce -> (received_at, set of requester host_ids) that asked us to relay
	# a ping (we forward the eventual ack to each). `received_at` is
	# `self.now_fn()` at the time of `handle_indirect_ping`, used by the
	# `check_timeouts` TTL sweep to drop stale entries whose ack never came
	# (§14.6 — otherwise the map grows without bound on dead targets).
	_pending_relays: dict[int, tuple[float, set[str]]] = field(default_factory=dict)
	# nonces we've already extended once (sent K indirect pings, re-armed to
	# `indirect_timeout`). The next miss for one of these → `mark_suspect`.
	_extended_nonces: set[int] = field(default_factory=set)

	# --- the round (prober side) --------------------------------------------

	def probe_round(
		self,
		daemon,
		transport: UdpTransport,
		*,
		nonces: Iterable[int] | None = None,
	) -> int:
		"""One probe tick (spec §14.2). Returns the number of peers probed.
		Indirect relays are sent only on missing ack (handled in
		`check_timeouts`); this round sends only direct pings. The target of
		each ping is one of `config.probe_peers` random alive members."""
		members = daemon.state.membership
		# Don't probe dead peers — they're already in the ladder past suspect.
		# The `select_peers` filter excludes non-`alive` wire records (a dead
		# host's wire record carries `state="alive"` from the origin — the
		# origin never sees itself as dead. So we ALSO filter via the tracker.)
		eligible = {
			h: m
			for h, m in members.items()
			if h != daemon.identity.host_id and self.tracker.state_of(h).value == "alive"
		}
		target_ids = select_peers(eligible, daemon.identity.host_id, self.config.probe_peers)
		sent = 0
		for target_id in target_ids:
			nonce = next(nonces) if nonces is not None else self._mint_nonce()
			self._send_ping(daemon, transport, target_id, nonce)
			# Arm: ack by `now + probe_timeout`, else try indirect relays.
			deadline = self.now_fn() + self.config.probe_timeout
			self.in_flight[nonce] = (target_id, deadline)
			sent += 1
		return sent

	def check_timeouts(self, daemon, transport: UdpTransport) -> list[str]:
		"""Called by the loop after each drain (between probe rounds): reconcile
		in-flight pings against the current clock. For each ping past its
		`probe_timeout` with no ack:

		  - if no indirect relays have been tried yet (nonce not in
		    `_extended_nonces`), send K indirect pings and extend the deadline
		    to `now + indirect_timeout`. The in_flight entry stays; the next
		    `check_timeouts` will reap it (indirect failed) if
		    `indirect_timeout` elapses with no ack.
		  - if the extended deadline (indirect_timeout) has elapsed too, mark
		    the peer `suspect` (ladder transition) and pop the in_flight.

		Returns the list of host_ids marked suspect this call (used by the
		loop's bookkeeping + a Stage 5 operator event).
		"""
		now = self.now_fn()
		# Stale-relay TTL sweep (§14.6): drop `_pending_relays` entries whose
		# ack never arrived past `indirect_timeout` — otherwise they accumulate
		# for dead targets (or a flooding peer), unbounded.
		for nonce in list(self._pending_relays.keys()):
			received_at, _ = self._pending_relays[nonce]
			if now - received_at > self.config.indirect_timeout:
				self._pending_relays.pop(nonce, None)
		marked_suspect: list[str] = []
		for nonce in list(self.in_flight.keys()):
			target_id, deadline = self.in_flight[nonce]
			if now < deadline:
				continue
			if nonce not in self._extended_nonces:
				# First miss → extend: send K indirect pings, re-arm.
				self._send_indirect_pings(daemon, transport, target_id, nonce)
				new_deadline = now + self.config.indirect_timeout
				self.in_flight[nonce] = (target_id, new_deadline)
				self._extended_nonces.add(nonce)
			else:
				# Second miss → suspect + pop.
				self.tracker.mark_suspect(target_id)
				marked_suspect.append(target_id)
				self.in_flight.pop(nonce, None)
				self._extended_nonces.discard(nonce)
		return marked_suspect

	# --- the handlers (responder side) ---------------------------------------

	def handle_ping(self, msg: Message, daemon, transport: UdpTransport) -> None:
		"""A direct ping arrived. Reply with an `ack` carrying the same
		(nonce, target). Replies go straight back to the sender's public
		endpoint — `unicast_send` on the daemon."""
		try:
			nonce, target = wire.parse_ping_payload(msg.payload)
		except (ValueError, KeyError):
			return
		if target != daemon.identity.host_id:
			# A ping whose target wasn't us — could be a misdirected direct
			# ping (a peer had a stale membership view). We don't answer on
			# behalf of someone else; drop.
			return
		ack = Message(
			type=TYPE_ACK,
			sender=daemon.identity.host_id,
			payload=wire.ack_payload(nonce, target),
		)
		requester_record = daemon.state.membership.get(msg.sender)
		if requester_record is None:
			return  # we don't know the requester; their own Membership
			# Advertisement will arrive and they'll retry.
		daemon.unicast_send(requester_record.endpoint, ack.to_bytes())

	def handle_ack(self, msg: Message, daemon, _transport: UdpTransport) -> None:
		"""A direct or relayed ack arrived. Match it to a pending ping by
		nonce; if matched, clear the in-flight (peer is alive). ALSO forward
		the ack to any relay requesters waiting on this nonce — a relay that
		received an `indirect_ping` from us is stashed in `_pending_relays`
		awaiting the target's ack to forward back to the requester."""
		try:
			nonce, _target = wire.parse_ack_payload(msg.payload)
		except (ValueError, KeyError):
			return
		# Direct ack path: match our own in-flight ping.
		if nonce in self.in_flight:
			self.in_flight.pop(nonce, None)
			self._extended_nonces.discard(nonce)
			self.tracker.note_alive(msg.sender)
		# Relay path: forward this ack to anyone waiting on it. We're a relay
		# in the middle; the ack came from the actual target via us. The
		# requester's `handle_ack` will match the nonce against ITS own
		# `in_flight` (the nonce was the requester's original — we preserved it
		# in the indirect_ping → ping → ack round trip).
		entry = self._pending_relays.pop(nonce, None)
		if entry is None:
			return
		_, requesters = entry
		for requester_id in requesters:
			requester_record = daemon.state.membership.get(requester_id)
			if requester_record is None:
				continue  # the requester left the cluster; drop the forwarded ack.
			ack_fwd = Message(
				type=TYPE_ACK,
				sender=daemon.identity.host_id,  # attackers can't forge: §19.3
				payload=wire.ack_payload(nonce, _target),
			)
			daemon.unicast_send(requester_record.endpoint, ack_fwd.to_bytes())

	def handle_indirect_ping(self, msg: Message, daemon, transport: UdpTransport) -> None:
		"""A peer asked us to ping `target` on its behalf (§14.2 step 3). We
		send a direct `ping` to the target with the requester's nonce; the
		target's `ack` will route back to us, and we forward it to the
		requester. Implementation here forwards the ack as soon as we receive
		it (the relay's ack handler matches the nonce back to the requester
		and unicast-replies). Simpler than a separate forward-ack path."""
		try:
			nonce, target_id, requester_id = wire.parse_indirect_ping_payload(msg.payload)
		except (ValueError, KeyError):
			return
		target_record = daemon.state.membership.get(target_id)
		if target_record is None:
			return  # we don't know the target — can't relay; the requester
			# will time out and mark suspect. Could reply with a NACK; SWIM
			# uses silence (simpler; the indirect_timeout catches it).
		# Record the (nonce, requester) so when the target's ack comes back we
		# know where to forward it. We stash these in a parallel dict so the
		# direct-ping's in_flight state (keyed on our own nonces, not this) is
		# untouched.
		entry = self._pending_relays.setdefault(nonce, (self.now_fn(), set()))
		entry[1].add(requester_id)
		# Send the ping to the target with the REQUESTER's nonce, so the
		# target's ack carries the same nonce and we can match.
		ping = Message(
			type=TYPE_PING,
			sender=daemon.identity.host_id,
			payload=wire.ping_payload(nonce, target_id),
		)
		daemon.unicast_send(target_record.endpoint, ping.to_bytes())

	# --- helpers --------------------------------------------------------------

	def _send_ping(self, daemon, transport, target_id: str, nonce: int) -> None:
		"""Direct ping to ``target_id``."""
		target = daemon.state.membership.get(target_id)
		if target is None:
			return  # the target was reaped between selection and send; skip.
		self.tracker.note_probed(target_id)
		msg = Message(
			type=TYPE_PING,
			sender=daemon.identity.host_id,
			payload=wire.ping_payload(nonce, target_id),
		)
		daemon.unicast_send(target.endpoint, msg.to_bytes())

	def _send_indirect_pings(self, daemon, transport, target_id: str, nonce: int) -> None:
		"""Forward the ping to `config.indirect_relays` random other peers with
		a request to relay to `target_id` on our behalf."""
		elig = {
			h
			for h in daemon.state.membership
			if h != daemon.identity.host_id and h != target_id and self.tracker.state_of(h).value == "alive"
		}
		relays = list(elig)
		# Uniformly random selection — Stage 4 ships uniform; Lifeguard's
		# health-aware sampler (§14.2 paragraph 2) is the same drop-in place
		# as for gossip peers (`peers.select_peers`).
		import random

		random.shuffle(relays)
		for relay_id in relays[: self.config.indirect_relays]:
			relay_record = daemon.state.membership.get(relay_id)
			if relay_record is None:
				continue
			req = Message(
				type=TYPE_INDIRECT_PING,
				sender=daemon.identity.host_id,
				payload=wire.indirect_ping_payload(nonce, target_id, daemon.identity.host_id),
			)
			daemon.unicast_send(relay_record.endpoint, req.to_bytes())

	def _mint_nonce(self) -> int:
		"""A 64-bit random nonce. `secrets` for cryptographic randomness —
		the nonce is only for ack correlation, but using `secrets` defends
		against a future where an attacker could forge an ack from a spoofed
		peer (the §19.3 signature check is the formal defence; this is a
		cheap extra layer)."""
		return secrets.randbits(64)


__all__ = ["ProbeProtocol"]
