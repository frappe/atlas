"""Cold-join sequence (spec §9.1).

A newcomer's first act is to dial every seed host with a
``MembershipAdvertisement`` unicast — just its own Membership Record — over
plain UDP to each seed's **public endpoint** (``seed.endpoint``, from
seed.json). The ANCP transport is bound to the host's own public IPv6, not to
the wg-mesh private /128, so no WireGuard peers need to exist yet — the
control plane (ANCP) is independent of the data plane (wg-mesh). Each seed
replies with a ``Gossip`` carrying (its own record + a bundle of every OTHER
known member's latest record), the state-transfer optimization that folds what
would otherwise be a second anti-entropy round into the join acknowledgement.
The reply handler in ``gossip.py`` applies all of them via the same monotonic
apply rule; the newcomer ends the join with the full cluster peer set + the
latest Membership Record for every origin.

If all seeds are partitioned / dead, ``cold_join`` retries every
``probe_interval`` until one answers (spec §9.2 last paragraph). Stage 2 wires
the retry inside ``cold_join`` itself; Stage 4 (SWIM) will fold the join
retry into the same ``probe_interval`` round that drives direct probes.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from . import wire
from .daemon import Daemon
from .records import MembershipRecord
from .transport import UdpTransport
from .wire import TYPE_MEMBERSHIP_ADVERT, Message


def cold_join(
	daemon: Daemon,
	transport: UdpTransport,
	seed_records: list[MembershipRecord],
	*,
	now_fn: Callable[[], float] = time.monotonic,
	retry_interval: float | None = None,
	max_attempts: int | None = None,
) -> int:
	"""Send our own Membership Advertisement to every seed (§9.1 step 4). Each
	unicast datagram is one Membership Advertisement. Seeds reply
	asynchronously via `handle_message` in the gossip loop — `cold_join` does
	NOT block on the replies (the replies arrive on the next loop tick's
	`drain`).

	Returns the number of seeds contacted (0 iff no seeds configured — the
	lone-host posture of §9.2). `retry_interval` and `max_attempts` are only
	consulted by `cold_join_with_retry` below — this simple form sends once per
	seed, fires-and-forgets; if no seed answers the daemon comes up peer-empty
	and relies on subsequent gossip / anti-entropy to fill in.

	Stage 5: the Membership Advertisement is signed with the daemon's own
	ed25519 signing key (§19.3) so the seed's verifier can confirm the
	cold-join origin's identity on top of the wg-transport binding. Stage 5+
	(§19.1): the envelope is whole-envelope-signed by the sender's key. Stage
	5+ (§19.5): if this host is a newcomer to an existing cluster (its own
	`host_id` is NOT in any older host's `seed.json`), the operator has
	additionally signed its `(host_id, signing_public_key, generation)`
	binding with the operator provision key; that signature rides the envelope
	as `introduction_signature`, and the seed's verifier accepts the
	self-asserted signing key ONLY against this operator cert. The
	introduction cert is pre-loaded by `main.py` from disk and stored on the
	daemon as `own_introduction_signature` (empty if absent — the host
	assumes it's part of the initial seed and seeds already trust its
	pubkey)."""
	_ = (retry_interval, max_attempts, now_fn)  # kept in the signature for Stage 4 wiring
	if transport.socket is None:
		raise RuntimeError("cold_join called before transport started")
	payload = wire.membership_advert_payload(daemon.own_membership)
	# Stage 5 — sign the inner MembershipRecord's wire dict (the per-record
	# signature, §19.3). The advert's origin IS us
	# (`own_membership.host_id == identity.host_id`).
	if daemon.own_signing_priv_b64:
		wire.attach_signature({"k": "m", "v": payload}, daemon.own_signing_priv_b64)
	# Stage 5+ — build the envelope. `signing_public_key` rides the wire so a
	# seed with no cached pubkey for us can verify against the self-asserted
	# key (gated by the §19.5 introduction cert below). `to_bytes(priv_b64)`
	# signs the canonical envelope body with our own signing key (§19.1).
	message = Message(
		type=TYPE_MEMBERSHIP_ADVERT,
		sender=daemon.identity.host_id,
		signing_public_key=daemon.own_signing_pub_b64,
		payload=payload,
		# §19.5 — the operator-signed introduction certificate. Empty when
		# the host was part of the initial seed (the cluster's other members
		# already have its pubkey anchored via their own seed.json; no
		# introduction needed). Non-empty when the host joined an existing
		# cluster post-bootstrap.
		introduction_signature=getattr(daemon, "own_introduction_signature", "") or "",
	)
	data = message.to_bytes(daemon.own_signing_priv_b64)
	sent = 0
	for seed in seed_records:
		try:
			transport.send((seed.endpoint, daemon.config.ancp_port), data)
			sent += 1
		except OSError:
			# The seed's endpoint is not reachable (seed down, network
			# partition). Swallow the error so the daemon doesn't crash on
			# startup — the regular loop's gossip + anti-entropy will retry.
			pass
	return sent


__all__ = ["cold_join"]
