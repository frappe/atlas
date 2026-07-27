"""Host-side "parked" reachability for a Sleeping VM (spec/32-sleepy-vms.md).

When a VM sleeps, its unit stops and vm-network-down.py tears down ALL of its
host networking — the proxy-NDP entry, the /128 route, the netns/veth/tap, and
the per-VM forward rules — so the VM's public IPv6 is completely unrouted. That
frees the RAM (the whole point of sleep) but also means an inbound TCP connection
can never reach the host to trigger a wake.

Park restores the MINIMUM reachability needed to TRAP the first inbound TCP SYN,
with no running guest:

  - proxy-NDP for the /128 on the uplink, so the upstream router keeps delivering
    the VM's packets to this host (exactly as vm-network-up would);
  - a /128 route out a shared, always-up dummy interface (atlas-park0), so an
    inbound packet is FORWARDED (traverses `inet atlas forward`) instead of being
    input-delivered and consumed by the host — the same off-link trick the
    reserved-IP DNAT relies on ("must NOT be a local address … leaving it off-link
    is what lets the packet be forwarded", reserved_ip_nat.py);
  - one forward rule matching a connection-opening TCP SYN to the /128, with a
    named counter, that DROPs it:

        ip6 daddr <vm> tcp flags syn / fin,syn,rst,ack counter name wake_<hex> drop

    `tcp flags syn / fin,syn,rst,ack` is nft's mask/value form — it matches only a
    packet with SYN set and FIN/RST/ACK clear (a genuine new-connection SYN, not a
    SYN-ACK, not a mid-stream segment). `tcp flags` implies TCP, so ICMP (ping) and
    UDP never match: they fall through to the chain's `policy accept`, are forwarded
    out the dummy, and are silently discarded WITHOUT waking (the "TCP only"
    contract). The SYN is DROPped, not rejected, so the client's TCP stack
    retransmits after its RTO (~1s) — by then atlas-wake-trap.py has started the
    unit and the real path is live, so the retransmit reaches the resumed guest.

atlas-wake-trap.py (the always-on host daemon) polls the named counters and, on
the first packet for a still-sleeping VM, does the local wake (remove the marker,
start the unit). The started unit's vm-network-up.py calls unpark() FIRST, so the
rule + counter + dummy route are gone before the real forwarding path comes up.

A NAMED counter, not the anonymous `counter` the forward accept rules use: only
named counters appear in `nft list counters`, the flat, cheap surface the daemon
polls (no whole-chain parse). The name is `wake_<uuid-no-dashes>` — nft
identifiers forbid `-`, and the hex is a pure function of the UUID, so the
uuid<->counter map needs no stored state.

Pure argv/string construction except park() / unpark() / ensure_park_device(),
which touch the host; the builders are unit-testable with bare `python3 -m
unittest`, like firewall.py / reserved_ip_nat.py.
"""

from __future__ import annotations

from atlas._run import _substitute, run, run_ok
from atlas.network_env import default_route_device, read_network_env_optional
from atlas.paths import VirtualMachinePaths

FORWARD = "forward"
# The shared, always-up dummy interface every sleeping VM's /128 routes out, so an
# inbound packet is forwarded (hits the forward hook) with no running guest. Created
# at bootstrap and re-asserted by ensure_park_device(); kept across a reset.
PARK_DEVICE = "atlas-park0"
_COUNTER_PREFIX = "wake_"


def counter_name(uuid: str) -> str:
	"""The named nft counter for a VM's parked SYN trap. nft identifiers forbid
	'-', so use the UUID hex; it is a pure function of the UUID (no stored map)."""
	return _COUNTER_PREFIX + uuid.replace("-", "")


def uuid_for_counter(name: str) -> str | None:
	"""Inverse of counter_name for the daemon: recover the dashed UUID from a
	`wake_<32hex>` counter name, or None when the name is not ours / malformed."""
	if not name.startswith(_COUNTER_PREFIX):
		return None
	hex_digits = name[len(_COUNTER_PREFIX) :]
	if len(hex_digits) != 32 or any(character not in "0123456789abcdef" for character in hex_digits):
		return None
	return (
		f"{hex_digits[0:8]}-{hex_digits[8:12]}-{hex_digits[12:16]}-"
		f"{hex_digits[16:20]}-{hex_digits[20:32]}"
	)


def counter_command(uuid: str) -> str:
	"""`nft add counter` for the VM's named SYN-trap counter (a table-scope object
	the daemon reads via `nft list counters`)."""
	return f"add counter inet atlas {counter_name(uuid)}"


def wake_rule_command(virtual_machine_ipv6: str, uuid: str) -> str:
	"""The forward rule: count + DROP a connection-opening TCP SYN to the VM's /128.
	`tcp flags syn / fin,syn,rst,ack` matches only a bare SYN (SYN set; FIN/RST/ACK
	clear), so ICMP/UDP and SYN-ACK do not match. drop (not reject) so the client
	retransmits into the woken VM. The counter name is a fixed hex identifier (not
	data), so it stays literal; only the VM's /128 goes through a quoted `{}` hole."""
	return _substitute(
		f"add rule inet atlas {FORWARD} ip6 daddr {{}} "
		f"tcp flags syn / fin,syn,rst,ack "
		f"counter name {counter_name(uuid)} drop",
		(virtual_machine_ipv6,),
	)


def ensure_park_device() -> None:
	"""Create the shared always-up atlas-park0 dummy if missing, and bring it up.
	Created at bootstrap; re-created here so a post-reboot re-park (atlas-wake-trap's
	startup sweep, before any VM unit rebuilds anything) still has a device to route
	the parked /128 out of. Idempotent."""
	if not run_ok("ip link show {}", PARK_DEVICE):
		run("sudo ip link add {} type dummy", PARK_DEVICE)
	run("sudo ip link set {} up", PARK_DEVICE)


def park(uuid: str) -> None:
	"""Install parked reachability + the TCP-SYN wake trap for a Sleeping VM.
	Idempotent and self-healing — called at sleep (after the unit stops) and at
	atlas-wake-trap boot re-sweep. Reads the VM's /128 from network.env; a VM with
	no network.env (never provisioned / already terminated) is a no-op."""
	env = read_network_env_optional(VirtualMachinePaths(uuid).network_env)
	virtual_machine_ipv6 = env.get("VIRTUAL_MACHINE_IPV6")
	if not virtual_machine_ipv6:
		return

	ensure_park_device()

	# Keep answering NDP for the /128 so the upstream router still delivers here,
	# and route it off-link out the dummy so an inbound packet is forwarded (hits
	# the forward hook) rather than consumed by the host. Both are `replace` —
	# idempotent, and a re-park after reboot rebuilds them from scratch.
	uplink = default_route_device("-6", tolerate_missing=True)
	if uplink:
		run("sudo ip -6 neigh replace proxy {} dev {}", virtual_machine_ipv6, uplink)
	run("sudo ip -6 route replace {} dev {}", f"{virtual_machine_ipv6}/128", PARK_DEVICE)

	# The named counter must exist before the rule references it. Guard both adds on
	# the live ruleset (substring / list-counter checks) so a re-park is a no-op and
	# never duplicates the rule (which would split the count across two entries).
	if not run_ok("sudo nft list counter inet atlas {}", counter_name(uuid)):
		run("sudo nft " + counter_command(uuid))
	forward = run("sudo nft list chain inet atlas {}", FORWARD, check=False)
	if counter_name(uuid) not in forward:
		run("sudo nft " + wake_rule_command(virtual_machine_ipv6, uuid))


def unpark(uuid: str) -> None:
	"""Remove a VM's parked state: the SYN-trap rule, its named counter, and the
	off-link /128 route out atlas-park0. Called at the top of vm-network-up.py
	(BEFORE the real netns is rebuilt, so the retransmitted SYN reaches the guest)
	and by vm-network-down.py (so a terminate cleans park artifacts even though the
	already-stopped unit's ExecStopPost will not re-run). Best-effort and idempotent:
	a VM that was never parked (an ordinary start) is a no-op. proxy-NDP is NOT
	touched here — vm-network-up re-`replace`s it, vm-network-down deletes it."""
	name = counter_name(uuid)
	# Delete the rule(s) BEFORE the counter — nft refuses to drop a counter a rule
	# still references. The counter name is unique per VM, so it is an exact
	# discriminator for the handle scrape (the pattern from vm-network-down.py).
	listing = run("sudo nft -a list chain inet atlas {}", FORWARD, check=False)
	for line in listing.splitlines():
		if name in line and "handle" in line:
			run("sudo nft delete rule inet atlas {} handle {}", FORWARD, line.split()[-1], check=False)
	run("sudo nft delete counter inet atlas {}", name, check=False)

	env = read_network_env_optional(VirtualMachinePaths(uuid).network_env)
	virtual_machine_ipv6 = env.get("VIRTUAL_MACHINE_IPV6")
	if virtual_machine_ipv6:
		run("sudo ip -6 route del {} dev {}", f"{virtual_machine_ipv6}/128", PARK_DEVICE, check=False)
