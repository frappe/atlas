"""Minimal sd_notify (systemd's ready/watchdog/stopping protocol) — stdlib only,
matching the spec's "no agent runs on the server" + Taste.md "don't import —
copy": we deliberately do NOT pull in `systemd` (the Python binding) for ~10
lines of AF_UNIX datagram code. Mirrors the same posture `host-mesh.service`
takes (oneshot, no notify) but extended for `Type=notify`.

The protocol: systemd sets `NOTIFY_SOCKET` in the service environment; the
daemon sends a single AF_UNIX datagram with newline-separated `KEY=VALUE`
fields. `READY=1` announces the service is up; `WATCHDOG=1` periodically pats
the `WatchdogSec=` timer; `STOPPING=1` announces an orderly shutdown so systemd
doesn't consider the unit failed when the main process exits.

No `main()` here — every helper is a pure function that writes to the socket
when `NOTIFY_SOCKET` is set and is a no-op when it isn't, so the daemon runs
unchanged under `python3 atlas.networkd.main` for development (the env var is
absent and the helpers short-circuit).
"""

from __future__ import annotations

import os
import socket

_READY = "READY=1"
_WATCHDOG = "WATCHDOG=1"
_STOPPING = "STOPPING=1"


def notify(message: str) -> bool:
	"""Send a single `KEY=VALUE` (or multi-field) sd_notify message. Returns True
	if sent, False if no `NOTIFY_SOCKET` is set (running outside systemd — silent
	no-op). Raises whatever the underlying socket raises on a real transport
	error so a misconfigured unit fails loud rather than silently."""
	addr = os.environ.get("NOTIFY_SOCKET")
	if not addr:
		return False
	# Abstract socket (Linux): systemd strips the leading '@' and replaces with
	# a NUL; otherwise it's a filesystem path under /run/systemd/...
	if addr.startswith("@"):
		addr = "\0" + addr[1:]
	payload = message.encode("utf-8")
	sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
	try:
		sock.connect(addr)
		sock.sendall(payload)
	finally:
		sock.close()
	return True


def ready() -> bool:
	"""Announce the daemon is up. systemd transitions the unit `active` on
	receipt of this."""
	return notify(_READY)


def watchdog() -> bool:
	"""Pat the watchdog. systemd's `WatchdogSec=` relaunches the daemon if it
	stops sending these. The loop calls this once per tick so it's bounded by
	the tick interval (well below the unit's `WatchdogSec`)."""
	return notify(_WATCHDOG)


def stopping() -> bool:
	"""Announce an orderly shutdown. systemd marks the unit `inactive` rather
	than `failed`; critical so `systemctl status` doesn't read a SIGTERM as a
	crash."""
	return notify(_STOPPING)


__all__ = ["notify", "ready", "stopping", "watchdog"]
