"""UDP transport for ANCP messages (spec §13 + §19.1).

ANCP rides plain UDP on public IPv6 addresses (the host's ``endpoint`` from
its identity, not the wg-mesh private /128). This is deliberate: the control
plane (membership, gossip, probes, anti-entropy) must NOT depend on WireGuard
being configured — WireGuard is the *output* of the control plane, not its
transport. The daemon binds a UDP socket on its own public ``endpoint``
(``identity.json::endpoint``); peers dial ``<peer endpoint>:<ancp_port>``
directly.

Stage 2 uses plain UDP send/recv — no reliability layer. Gossip is fire-and-
forget (anti-entropy is the backstop, §15); SWIM probes (§14) carry their own
ack/timeouts. A future quic-UDP variant is a §20 transport swap, not a change
to the wire or the dispatch table.

``UdpTransport`` is the small wrapper that owns the socket. The ``Daemon``
injects it so tests can swap in a pair of in-memory queues (no port, no kernel
socket) and prove the gossip round end-to-end without touching the network.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass, field

# The fixed UDP port ANCP listens on, region-wide. Unlike WireGuard's 51820
# (which needs a firewall rule), ANCP rides UDP on a distinct port (7946) and
# is reached at each host's public ``endpoint`` directly — no wg-mesh layer
# in the middle. The mgmt-firewall must allow ``udp dport {ancp_port}``
# inbound to every host.
from .config import DEFAULT_ANCP_PORT  # re-exported for callers
from .wire import MAX_DATAGRAM_BYTES, Message, from_bytes


@dataclass(slots=True)
class UdpTransport:
	"""One ANCP UDP socket per host. ``bind`` is the ``(public_ipv6, port)`` the
	socket listens on; ``send(target, bytes)`` ships a datagram to a peer's
	``(public_ipv6, ancp_port)``. Non-blocking recv so the loop's tick never
	stalls."""

	bind: tuple[str, int]
	socket: socket.socket | None = field(default=None, init=False)

	def start(self) -> None:
		"""Open and bind the UDP socket on the host's public IPv6 endpoint. Sets
		non-blocking so ``recv`` returns immediately when no datagram is pending
		(the loop's tick polls once per ``gossip_interval``). Raises on bind
		failure (port in-use, endpoint not assigned to a local interface) — fail
		loud at startup."""
		self.socket = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
		self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		self.socket.bind(self.bind)
		self.socket.setblocking(False)

	def stop(self) -> None:
		"""Close the socket. Idempotent so a `systemctl stop` after a partial
		startup doesn't double-close."""
		if self.socket is not None:
			self.socket.close()
			self.socket = None

	def send(self, target: tuple[str, int], data: bytes) -> None:
		"""Send a serialized Message to ``(endpoint, port)``. Raises if the
		socket is closed or the target is malformed — ``target[0]`` must be a
		valid IPv6 (the peer's public endpoint)."""
		if self.socket is None:
			raise RuntimeError("UdpTransport.send before .start()")
		if len(data) > MAX_DATAGRAM_BYTES:
			# The wire layer sends a `DatagramTooLarge` and the gossip handler
			# trims; this is the backstop check at the transport itself.
			raise ValueError(f"datagram size {len(data)} > {MAX_DATAGRAM_BYTES}")
		self.socket.sendto(data, target)

	def drain(self, handler: Callable[[Message, tuple[str, int]], None]) -> int:
		"""Non-blocking: recv every pending datagram, dispatch to `handler`.
		Returns the count processed. Stops at the first EWOULDBLOCK (the kernel
		queue is drained for this tick). A malformed datagram raises
		`ValueError` from the wire layer; the caller decides whether to swallow
		it (drop + log) or surface it. We swallow here — one bad byte from a
		peer shouldn't crash the loop; the operator's log surfaces it."""
		assert self.socket is not None  # caller invariant: started before drain
		count = 0
		while True:
			try:
				data, addr = self.socket.recvfrom(MAX_DATAGRAM_BYTES + 1)
			except BlockingIOError:
				break
			if len(data) > MAX_DATAGRAM_BYTES:
				continue  # oversized datagram — silently truncated by the kernel
			count += 1
			try:
				msg = from_bytes(data)
			except ValueError:
				# Drop + continue — a malformed datagram is logged at operator
				# level once we wire structured logging; the loop stays alive.
				continue
			handler(msg, addr)
		return count


__all__ = ["DEFAULT_ANCP_PORT", "MAX_DATAGRAM_BYTES", "Message", "UdpTransport", "from_bytes"]
