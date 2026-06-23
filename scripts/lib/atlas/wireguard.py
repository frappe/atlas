"""Host-side WireGuard tunnel plumbing for the VPN broker (spec/19-vpn-broker.md).

Each tunnel terminates on the host with its **own** wg interface in the host root
netns, listening on a per-tunnel UDP port on the server's public address. The
interface routes a decrypted packet destined to the VM's `/128` into that VM's
namespace over the host route `vm-network-up.py` already laid down — so reaching
the VM needs no new routing. A point-to-point `/127` overlay on the interface
gives the host and client ends private v6 addresses, and its connected route is
the VM's return path to the client.

**Isolation** — the one thing that makes "only your VM" true — is two rules in
the existing `inet atlas forward` chain, keyed on the interface name (which is
1:1 with the tunnel):

    iifname <iface> ip6 daddr <vm-v6> accept
    iifname <iface> drop

WireGuard's cryptokey routing only governs what the host sends *back* to a peer;
it does not restrict the *destination* of a decrypted inbound packet, so without
the `drop` a client could address another VM and the host would route it. The
`drop` closes that — anything from this interface that is not this VM is dropped.

These two rules must sit at the **head** of the forward chain, above the broad
per-VM forward-accept rules vm-network-up.py lays down (`ip6 daddr <vm> oifname
<veth> accept`). Those rules do not constrain the input interface, so an appended
tunnel `drop` is shadowed: a packet from this tunnel to *another* VM matches that
VM's accept first (accept is terminal) and is forwarded — the exact leak the drop
exists to stop. So we `insert` the pair (drop first, then accept, leaving
[accept, drop, …per-VM…]) instead of appending it.

Everything here is pure string/argv construction except `apply_tunnel` /
`remove_tunnel` / `apply_persisted_tunnels`, which touch the host. The rule and
command *generation*, and the `TunnelConfig` (de)serialization, are unit-testable
with bare `python3 -m unittest` (no host), like `reserved_ip_nat`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from atlas._run import run, run_ok
from atlas.network_env import NetworkEnv

TABLE = ("inet", "atlas")
FORWARD = "forward"


@dataclass(frozen=True)
class TunnelConfig:
	"""One tunnel's durable host state — everything `apply_tunnel` needs, and what
	a `<tunnel>.env` sidecar under the VM's `tunnels/` directory persists so a cold
	boot re-creates the tunnel exactly (the reserved-IP `network.env` pattern).

	The private key lives in its own 0600 file (`private_key_path`), not inline, so
	the metadata sidecar can stay 0644 and `wg set` reads the secret from a path."""

	interface: str
	listen_port: int
	private_key_path: str
	client_public_key: str
	client_address: str  # bare overlay v6 the client carries; the peer's allowed-ip
	host_address: str  # the host end's /127 overlay CIDR (its connected route is the return path)
	virtual_machine_ipv6: str  # the VM's /128 (bare) — the one destination the tunnel may reach

	@classmethod
	def from_env(cls, env: NetworkEnv) -> "TunnelConfig":
		"""Build from a parsed sidecar, failing loud (via require) on any missing key."""
		return cls(
			interface=env.require("INTERFACE"),
			listen_port=env.require_int("LISTEN_PORT"),
			private_key_path=env.require("PRIVATE_KEY_FILE"),
			client_public_key=env.require("CLIENT_PUBLIC_KEY"),
			client_address=env.require("CLIENT_ADDRESS"),
			host_address=env.require("HOST_ADDRESS"),
			virtual_machine_ipv6=env.require("VIRTUAL_MACHINE_IPV6"),
		)

	def to_env_text(self) -> str:
		"""Render the KEY=value sidecar (the inverse of from_env). Bare values, one
		per line, like provision's network.env."""
		return (
			"\n".join(
				[
					f"INTERFACE={self.interface}",
					f"LISTEN_PORT={self.listen_port}",
					f"PRIVATE_KEY_FILE={self.private_key_path}",
					f"CLIENT_PUBLIC_KEY={self.client_public_key}",
					f"CLIENT_ADDRESS={self.client_address}",
					f"HOST_ADDRESS={self.host_address}",
					f"VIRTUAL_MACHINE_IPV6={self.virtual_machine_ipv6}",
				]
			)
			+ "\n"
		)


def link_add_argv(interface: str) -> list[str]:
	"""Create the WireGuard interface in the current (host root) netns."""
	return ["ip", "link", "add", interface, "type", "wireguard"]


def link_up_argv(interface: str) -> list[str]:
	return ["ip", "link", "set", interface, "up"]


def link_del_argv(interface: str) -> list[str]:
	"""Remove the interface — takes its addresses and connected routes with it."""
	return ["ip", "link", "del", interface]


def addr_add_argv(interface: str, host_cidr: str) -> list[str]:
	"""Assign the host end's /127 overlay address. The kernel's connected route for
	the /127 is the VM's return path to the client end (the upper address), so no
	explicit per-client route is needed."""
	return ["ip", "-6", "addr", "add", host_cidr, "dev", interface]


def wg_set_interface_argv(interface: str, listen_port: int, private_key_path: str) -> list[str]:
	"""Set the listen port and private key (read from a 0600 file, never inline)."""
	return ["wg", "set", interface, "listen-port", str(listen_port), "private-key", private_key_path]


def wg_set_peer_argv(interface: str, client_public_key: str, client_address: str) -> list[str]:
	"""Add the one client peer, allowed-ips scoped to its overlay /128. Cryptokey
	routing then accepts inbound only when its inner source is this address, and
	sends return traffic only to this peer."""
	return ["wg", "set", interface, "peer", client_public_key, "allowed-ips", f"{client_address}/128"]


def accept_rule_argv(interface: str, virtual_machine_ipv6: str) -> list[str]:
	"""forward: accept decrypted tunnel traffic destined to the VM. Inserted at the
	head AFTER the drop, so it ends up just above it ([accept, drop, …]); the VM is
	reachable while everything else from the interface falls to the drop."""
	return [
		"insert", "rule", "inet", "atlas", FORWARD,
		"iifname", interface, "ip6", "daddr", virtual_machine_ipv6, "accept",
	]  # fmt: skip


def drop_rule_argv(interface: str) -> list[str]:
	"""forward: drop anything else arriving on this tunnel's interface — another
	VM, the host, the internet. This is the isolation guarantee; without it a
	client could address a VM that is not its own and the host would route it.
	Inserted at the head (above the per-VM accepts that would otherwise shadow it)."""
	return ["insert", "rule", "inet", "atlas", FORWARD, "iifname", interface, "drop"]


def apply_tunnel(config: TunnelConfig) -> None:
	"""Idempotently bring the tunnel up: create the interface, set its key/port and
	the one peer, assign the host overlay address, raise it, and install the two
	isolation rules. Re-running (cold boot, reconcile, double apply) is a no-op —
	the same self-healing contract as vm-network-up.py / reserved_ip_nat."""
	if not run_ok("sudo", "ip", "link", "show", config.interface):
		run("sudo", *link_add_argv(config.interface))
	run("sudo", *wg_set_interface_argv(config.interface, config.listen_port, config.private_key_path))
	run("sudo", *wg_set_peer_argv(config.interface, config.client_public_key, config.client_address))
	addresses = run("sudo", "ip", "-6", "addr", "show", "dev", config.interface, check=False)
	if config.host_address not in addresses:
		run("sudo", *addr_add_argv(config.interface, config.host_address))
	run("sudo", *link_up_argv(config.interface))

	# The forward chain is created by the vm-network-up scaffold (which runs first,
	# as ExecStartPre); guard defensively so a tunnel apply is self-sufficient.
	if not run_ok("sudo", "nft", "list", "chain", *TABLE, FORWARD):
		run(
			"sudo",
			"nft",
			"add chain inet atlas forward { type filter hook forward priority filter; policy accept; }",
		)
	forward = run("sudo", "nft", "list", "chain", *TABLE, FORWARD)
	# Insert at the head so the pair precedes the broad per-VM accepts. Each insert
	# goes to the top, so insert drop FIRST and accept SECOND to leave the chain as
	# [accept, drop, …per-VM…] — accept reachable, everything else dropped.
	if not _has_drop(forward, config.interface):
		run("sudo", "nft", *drop_rule_argv(config.interface))
	if not _has_accept(forward, config.interface, config.virtual_machine_ipv6):
		run("sudo", "nft", *accept_rule_argv(config.interface, config.virtual_machine_ipv6))


def remove_tunnel(interface: str) -> None:
	"""Tear the tunnel down, best-effort and idempotent: delete the two forward
	rules by handle (keyed on the interface name), then delete the interface (which
	takes its addresses and connected route with it). A missing rule or interface is
	not an error — a revoke may run after the VM is already gone, symmetric with
	vm-network-down.py."""
	listing = run("sudo", "nft", "-a", "list", "chain", *TABLE, FORWARD, check=False)
	for handle in _handles_for(listing, interface):
		run("sudo", "nft", "delete", "rule", "inet", "atlas", FORWARD, "handle", handle, check=False)
	run("sudo", *link_del_argv(interface), check=False)


def apply_persisted_tunnels(tunnels_directory: str) -> None:
	"""Re-apply every persisted tunnel for a VM at cold boot. Called by
	vm-network-up.py AFTER the netns/veth and the VM's `/128` host route exist, so
	each tunnel comes up functional. No directory (a VM with no tunnels) is a no-op.

	Fail-loud per tunnel (apply_tunnel raises): a tunnel that cannot be re-created
	is a real fault the operator retries by re-running, like the rest of the unit's
	ExecStartPre."""
	if not os.path.isdir(tunnels_directory):
		return
	for entry in sorted(os.listdir(tunnels_directory)):
		if not entry.endswith(".env"):
			continue
		path = os.path.join(tunnels_directory, entry)
		# nosemgrep: frappe-security-file-traversal -- host script; reads a per-VM tunnel sidecar path, not untrusted web input
		with open(path) as handle:
			config = TunnelConfig.from_env(NetworkEnv.parse(handle.read()))
		apply_tunnel(config)


def _has_accept(listing: str, interface: str, virtual_machine_ipv6: str) -> bool:
	return any(
		interface in line and virtual_machine_ipv6 in line and "accept" in line
		for line in listing.splitlines()
	)


def _has_drop(listing: str, interface: str) -> bool:
	return any(interface in line and "drop" in line for line in listing.splitlines())


def _handles_for(listing: str, interface: str):
	"""Trailing handle number of every forward rule mentioning this interface.
	`nft -a` prints `… # handle N`; the handle is the last token. Mirrors the
	handle-scrape in vm-network-down.py / reserved_ip_nat."""
	for line in listing.splitlines():
		if interface in line and "handle" in line:
			yield line.split()[-1]
