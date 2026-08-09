"""Host-side public-ingress firewall for a VM (spec/20-firewall.md).

A VM's public IPv6 is reachable from the whole internet by default (the
`inet atlas forward` chain is `policy accept` with a broad per-VM accept). A
Firewall restricts that **public** surface to a chosen set of ports — "only 443",
say — while leaving two paths untouched:

  - the VPN tunnel, which keeps FULL access to the whole VM, and
  - the VM's own outbound connections (their return traffic is not new ingress).

The mechanism is a SEPARATE, higher-priority base chain on the same hook:

    chain public_filter {
        type filter hook forward priority filter - 5; policy accept;
        iifname <uplink> ip6 daddr <vm> ct state established,related accept
        iifname <uplink> ip6 daddr <vm> tcp dport <p> accept   # one per allowed rule
        iifname <uplink> ip6 daddr <vm> drop                   # public, not allowed -> DROP
    }

Why this shape gives "VPN bypasses the firewall" for free: every rule is scoped to
`iifname <uplink>` (the host's public NIC). Tunnel traffic arrives on a `wg-…`
interface, never the uplink, so it matches nothing here and falls through to the
`forward` chain (where the tunnel's own accept/drop govern it). A `drop` in this
earlier chain is terminal; an `accept` only ends THIS chain, so allowed traffic
proceeds to `forward` and is delivered as before. The `established,related` accept
turns conntrack on for the forward hook — the one real behavioral addition — so a
reply to a connection the VM opened is never mistaken for new public ingress.

No Firewall attached → no sidecar → no rules here → the VM stays fully public
(opt-in, per spec/20). Everything is pure argv/string construction except
`apply_firewall` / `remove_firewall` / `apply_persisted_firewall`, which touch the
host; the rest is unit-testable with bare `python3 -m unittest`, like wireguard.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from atlas._run import _substitute, run, run_ok
from atlas.network_env import NetworkEnv, default_route_device

TABLE = ("inet", "atlas")
PUBLIC_FILTER = "public_filter"
# Runs BEFORE the forward chain (priority 0), so its drop pre-empts forward's broad
# per-VM accept. nft accepts arithmetic on the named base priority.
PUBLIC_FILTER_PRIORITY = "filter - 5"
PROTOCOLS = ("tcp", "udp")


@dataclass(frozen=True)
class Rule:
	"""One allowed public ingress rule: a transport protocol and a destination port."""

	protocol: str
	port: int

	@classmethod
	def parse(cls, token: str) -> "Rule":
		"""Parse a `proto/port` token (`tcp/443`), failing loud on anything invalid —
		these come from a typed flag, but the host never trusts its input blindly."""
		protocol, separator, port = token.partition("/")
		if not separator or protocol not in PROTOCOLS:
			raise SystemExit(f"firewall rule {token!r}: expected one of {PROTOCOLS} as proto/port")
		try:
			number = int(port)
		except ValueError:
			raise SystemExit(f"firewall rule {token!r}: port is not an integer")
		if not 1 <= number <= 65535:
			raise SystemExit(f"firewall rule {token!r}: port {number} out of range 1-65535")
		return cls(protocol=protocol, port=number)

	def token(self) -> str:
		return f"{self.protocol}/{self.port}"


@dataclass(frozen=True)
class FirewallConfig:
	"""One VM's durable public-firewall state — what `apply_firewall` needs and what
	the `firewall.env` sidecar persists so a cold boot re-applies it (the network.env
	pattern). An empty `rules` is meaningful: a firewall with no open ports denies all
	public ingress (the VM is then reachable only over its VPN tunnel)."""

	virtual_machine_ipv6: str
	rules: tuple[Rule, ...]

	@classmethod
	def from_env(cls, env: NetworkEnv) -> "FirewallConfig":
		"""Build from a parsed sidecar. RULES is a space-separated list of proto/port
		tokens; absent or empty means deny-all-public."""
		tokens = env.get("RULES").split()
		return cls(
			virtual_machine_ipv6=env.require("VIRTUAL_MACHINE_IPV6"),
			rules=tuple(Rule.parse(token) for token in tokens),
		)

	def to_env_text(self) -> str:
		"""Render the KEY=value sidecar (the inverse of from_env). Bare values, like
		provision's network.env."""
		rules = " ".join(rule.token() for rule in self.rules)
		return f"VIRTUAL_MACHINE_IPV6={self.virtual_machine_ipv6}\nRULES={rules}\n"


def ensure_chain_command() -> str:
	"""The rendered `add chain` command string. nft's brace clause must reach nft as
	ONE argv token (Trap 2), so it goes through a `{}` hole — quoted — while the rest
	is literal. Idempotency is the caller's `run_ok` guard, as elsewhere."""
	clause = f"{{ type filter hook forward priority {PUBLIC_FILTER_PRIORITY}; policy accept; }}"
	return _substitute(f"add chain inet atlas {PUBLIC_FILTER} {{}}", (clause,))


def established_rule_command(uplink: str, virtual_machine_ipv6: str) -> str:
	"""Accept replies to connections the VM itself opened — they arrive from the
	uplink destined to the VM but are not new public ingress. Must precede the drop.
	Returns a rendered command string (uplink/v6 quoted as single tokens)."""
	return _substitute(
		f"add rule inet atlas {PUBLIC_FILTER} iifname {{}} ip6 daddr {{}} "
		f"ct state established,related accept",
		(uplink, virtual_machine_ipv6),
	)


def port_rule_command(uplink: str, virtual_machine_ipv6: str, rule: Rule) -> str:
	"""Accept new public ingress to one allowed protocol/port on the VM."""
	return _substitute(
		f"add rule inet atlas {PUBLIC_FILTER} iifname {{}} ip6 daddr {{}} {{}} dport {{}} accept",
		(uplink, virtual_machine_ipv6, rule.protocol, rule.port),
	)


def drop_rule_command(uplink: str, virtual_machine_ipv6: str) -> str:
	"""Drop everything else arriving from the uplink for this VM — the closing rule
	of the VM's block, so any public port not explicitly allowed is unreachable."""
	return _substitute(
		f"add rule inet atlas {PUBLIC_FILTER} iifname {{}} ip6 daddr {{}} drop",
		(uplink, virtual_machine_ipv6),
	)


def apply_firewall(config: FirewallConfig) -> None:
	"""Idempotently install this VM's public-ingress block. Discovers the host's v6
	uplink fresh (like vm-network-up), ensures the chain exists, removes any existing
	rules for this VM, then appends the block in order: established accept, one accept
	per allowed rule, drop. Re-running (cold boot, edit, retry) converges to the same
	block — the self-healing contract shared with apply_tunnel / reserved_ip_nat."""
	uplink = default_route_device("-6")
	if not run_ok("sudo nft list chain {} {} {}", *TABLE, PUBLIC_FILTER):
		run("sudo nft " + ensure_chain_command())
	_clear_vm_rules(config.virtual_machine_ipv6)
	run("sudo nft " + established_rule_command(uplink, config.virtual_machine_ipv6))
	for rule in config.rules:
		run("sudo nft " + port_rule_command(uplink, config.virtual_machine_ipv6, rule))
	run("sudo nft " + drop_rule_command(uplink, config.virtual_machine_ipv6))


def remove_firewall(virtual_machine_ipv6: str) -> None:
	"""Delete this VM's block, reverting it to fully public (the forward chain's broad
	accept takes over again). Best-effort and idempotent: a missing chain or absent
	block is not an error — a detach may run after the VM is already gone, symmetric
	with vm-network-down.py."""
	if not run_ok("sudo nft list chain {} {} {}", *TABLE, PUBLIC_FILTER):
		return
	_clear_vm_rules(virtual_machine_ipv6)


def apply_persisted_firewall(firewall_env_path: str) -> None:
	"""Re-apply a VM's firewall at cold boot from its sidecar. Called by
	vm-network-up.py AFTER the VM's /128 host route exists. No sidecar (no firewall
	attached) is a no-op — the VM stays public. Fail-loud (apply raises) like the rest
	of the unit's ExecStartPre."""
	if not os.path.isfile(firewall_env_path):
		return
	# nosemgrep: frappe-security-file-traversal -- host script; reads a per-VM firewall sidecar path, not untrusted web input
	with open(firewall_env_path) as handle:
		config = FirewallConfig.from_env(NetworkEnv.parse(handle.read()))
	apply_firewall(config)


def _clear_vm_rules(virtual_machine_ipv6: str) -> None:
	"""Delete every public_filter rule for this VM by handle. The chain is dedicated
	to public filtering and every rule is daddr-scoped to one VM, so the VM's address
	is an exact discriminator — the handle-scrape pattern from wireguard/reserved_ip."""
	listing = run("sudo nft -a list chain {} {} {}", *TABLE, PUBLIC_FILTER, check=False)
	for handle in _handles_for(listing, virtual_machine_ipv6):
		run("sudo nft delete rule inet atlas {} handle {}", PUBLIC_FILTER, handle, check=False)


def _handles_for(listing: str, virtual_machine_ipv6: str):
	"""Trailing handle of every public_filter rule mentioning this VM's address.
	`nft -a` prints `… # handle N`; the handle is the last token."""
	for line in listing.splitlines():
		if virtual_machine_ipv6 in line and "handle" in line:
			yield line.split()[-1]
