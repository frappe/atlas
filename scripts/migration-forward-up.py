#!/usr/bin/env python3
# Bring up one end of a VM migration's keep-address forward tunnel (spec/24 §2.9).
#
# When a VM migrates keeping its /128, the source host keeps holding the /64 the
# /128 is carved from — so it keeps receiving the VM's inbound traffic — and it
# forwards that traffic to the target over a per-VM point-to-point tunnel. This
# script brings up ONE end of that tunnel; the controller runs it on BOTH hosts
# in the TargetPreparing phase (source first as the listener, then target as the
# connector). The route installs that make traffic actually flow come later, at
# cutover (migration-source-forward.py / migration-target-receive.py).
#
# The tunnel is a `tun` device (one L3 family — the inner IPv6 /128) whose frames
# socat bridges to a plain TCP stream between the two hosts. STAGE transport is
# UNENCRYPTED plain TCP, matching the stage-1 NBD path (a secure host-to-host
# carrier is a deferred follow-up, spec/24 §2.1). The device name and TCP port
# are pure functions of the VM UUID, so both hosts derive them identically and a
# lost-task re-entry needs no stored state.
#
# Idempotent: an already-up tun device with a live socat is left alone; a
# re-entry re-asserts the address, MTU, and forwarding sysctl (all cheap no-ops
# if already set) and only (re)starts socat if it is not running.
#
# Inputs:
#   virtual_machine_name  - UUID (keys the device/port; also the tun-name)
#   role                  - "source" (TCP listener) or "target" (TCP connector)
#   tunnel_device         - the mig6-<vm8> interface name (controller-derived)
#   tunnel_port           - the localhost/peer TCP port for the socat carrier
#   source_host           - the source's reachable address (target role only;
#                           what the connector dials). Empty on the source.
#
# Emits ATLAS_RESULT={"tunnel_device": "...", "up": true}

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import run, run_ok
from atlas._task import TaskInputs, TaskResult

# The tunnel carries exactly one inner family (the VM's IPv6 /128). Pin the MTU
# to the IPv6 minimum so the socat/TCP encapsulation never triggers in-tunnel
# PMTU surprises under live load (spec/24 §2.1). The two ends address the tunnel
# with a link-local /64 (fe80::a source / fe80::b target) purely so the device is
# "up with an address"; the guest's /128 is routed over it, not assigned on it.
TUNNEL_MTU = 1280
SOURCE_LINK_LOCAL = "fe80::a/64"
TARGET_LINK_LOCAL = "fe80::b/64"


@dataclass(frozen=True)
class ForwardUpInputs(TaskInputs):
	"""Bring up one end of a keep-address migration forward tunnel."""

	command: typing.ClassVar[str] = "migration-forward-up"
	virtual_machine_name: str
	role: str
	tunnel_device: str
	tunnel_port: int
	source_host: str = ""


@dataclass(frozen=True)
class ForwardUpResult(TaskResult):
	tunnel_device: str
	up: bool = True


def main() -> None:
	inputs = ForwardUpInputs.from_args()
	if inputs.role not in ("source", "target"):
		sys.exit(f"role must be 'source' or 'target', got {inputs.role!r}")
	if inputs.role == "target" and not inputs.source_host:
		sys.exit("target role requires --source-host (the address the connector dials)")

	# Forwarding across the tunnel<->veth seam. Set at bootstrap and re-applied by
	# vm-network-up.py; a defensive re-assert here costs nothing and covers a host
	# that came up between bootstrap and this migration.
	run("sudo sysctl -q -w net.ipv6.conf.all.forwarding=1", check=False)

	_ensure_socat(inputs)
	_address_tunnel(inputs)

	ForwardUpResult(tunnel_device=inputs.tunnel_device).emit()
	print(f"Forward tunnel {inputs.tunnel_device} up on the {inputs.role} side (port {inputs.tunnel_port}).")


def _ensure_socat(inputs: ForwardUpInputs) -> None:
	"""(Re)start the socat that owns the tun device and bridges it to the TCP
	stream. socat CREATES the tun device (tun-name=…), so the device's existence
	and the carrier's liveness are the same fact — we key idempotency on the unit.

	**TUN is address 1 (verified on the real hosts).** socat opens address 1
	immediately at startup but opens address 2 only once address 1 is established;
	with a `TCP-LISTEN` first, the TUN (address 2) would not be created until a peer
	connected — a deadlock, since the peer's own TUN waits the same way. Putting the
	TUN first makes the device appear the instant socat starts, on both ends, before
	any connection. No `fork` on the listener: this is ONE point-to-point tunnel per
	VM, so a single accepted connection bridges the single TUN; fork would spawn a
	second TUN per connection. iff-up brings the device up; iff-no-pi drops the
	4-byte packet-info header so the stream is pure IP.

	Source LISTENS (TCP-LISTEN, reuseaddr so a re-entry rebinds cleanly); target
	CONNECTS (TCP:<source>:<port>, retry+forever so it rides out the source
	not-yet-listening and any mid-window blip)."""
	unit = _unit_name(inputs.tunnel_port)
	if _socat_alive(unit):
		return

	tun = f"TUN,tun-name={inputs.tunnel_device},iff-up,iff-no-pi"
	if inputs.role == "source":
		endpoint = f"TCP-LISTEN:{inputs.tunnel_port},bind=0.0.0.0,reuseaddr"
	else:
		endpoint = f"TCP:{inputs.source_host}:{inputs.tunnel_port},retry=5,forever"

	# systemd-run runs socat as a transient unit's OWN main process (no bash -c
	# wrapper, no pidfile) — that fully detaches it from this SSH session (a bare
	# `&` dies on session close, verified on the real hosts for the NBD listener) and
	# makes `systemctl is-active <unit>` the single source of truth for liveness and
	# teardown. --unit keeps the name stable so a re-entry finds it; reset-failed
	# clears a prior crashed instance so the name is reusable. socat's argv is passed
	# as separate tokens after `--` (TUN FIRST — see above) so systemd-run execs it.
	run("sudo systemctl reset-failed {}", unit, check=False)
	run(
		"sudo systemd-run --unit={} --property=Type=simple --collect -- socat {} {}",
		unit,
		tun,
		endpoint,
	)


def _address_tunnel(inputs: ForwardUpInputs) -> None:
	"""Address the tun device with a link-local /64 and pin the MTU. socat brought
	it up (iff-up), but the address + MTU are ours to set. `addr replace` and `link
	set` are idempotent, so a re-entry just re-asserts. We wait briefly for socat to
	create the device on a cold start."""
	link_local = SOURCE_LINK_LOCAL if inputs.role == "source" else TARGET_LINK_LOCAL
	for _ in range(50):
		if run_ok("ip link show {}", inputs.tunnel_device):
			break
		run("sleep 0.1", check=False)
	else:
		sys.exit(f"socat did not create tun device {inputs.tunnel_device} in time")
	run("sudo ip -6 addr replace {} dev {} nodad", link_local, inputs.tunnel_device)
	run("sudo ip link set {} mtu {} up", inputs.tunnel_device, str(TUNNEL_MTU))


def _socat_alive(unit: str) -> bool:
	"""True if the socat transient unit is active. The tun device lives and dies with
	that process, so this is also 'is the device up?'."""
	return run("sudo systemctl is-active {}", unit, check=False).strip() == "active"


def _unit_name(port: int) -> str:
	"""The transient unit name for a tunnel's socat carrier, keyed on its port so
	the up/down/liveness paths all name the same unit with no stored state."""
	return f"atlas-mig6-{port}"


if __name__ == "__main__":
	main()
