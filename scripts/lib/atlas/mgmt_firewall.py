"""Host management-plane firewall for an Atlas host (nftables, spec/21-tunnel.md).

Default-deny inbound on the **public interface** except the WireGuard UDP port (the
one thing that lets the Central hub dial in), loopback, established/related, ICMP, and
an operator-configurable `public_allow_ports` list (default empty). Every non-public
interface — `wg0`, loopback, a private NIC — is left wide open (policy accept), so
Frappe/SSH over the tunnel are reachable while the public side is dark.

This is a SEPARATE nft table (`inet atlas_mgmt`) from the data-plane `inet atlas`
table (06-networking.md): this one only hooks `input` (host-destined traffic), never
`forward` (VM traffic), so locking down the management plane never touches a hosted
site. Both tables coexist at their hooks.

Lockout safety: `apply` arms a systemd-run transient timer that **reverts** (deletes
this table, restoring open access) after N seconds unless `confirm` cancels it first.
A failed handoff therefore can never permanently lock Central — or the operator — out.

Pure string/argv construction (`mgmt_ruleset`, `loadable_ruleset`) is unit-testable
with bare `python3 -m unittest`; only `apply`/`arm_revert`/`cancel_revert`/`revert`/
`persist` touch the host.
"""

from __future__ import annotations

from atlas._run import install_directory, install_file, run

TABLE = "atlas_mgmt"
REVERT_UNIT = "atlas-firewall-revert"
# The confirmed ruleset is loaded at boot by the operator-staged
# atlas-mgmt-firewall.service (ordered Before=network-pre.target — fail-closed, no
# public-exposure window). Writing/removing this file + enabling/disabling that unit
# is how confirm/revert make the lockdown survive (or not survive) a reboot.
PERSIST_PATH = "/etc/atlas/mgmt-firewall.nft"
PERSIST_UNIT = "atlas-mgmt-firewall.service"


# --- pure builders (unit-testable, no host) ---------------------------------


def mgmt_ruleset(public_interface: str, wg_port: int, public_allow_ports: list[str] | None = None) -> str:
	"""The `table inet atlas_mgmt { … }` text. Only the public interface jumps to the
	drop chain; everything else rides the `policy accept`. The accept order matters:
	established/related first (so the host's own outbound gets replies), then the
	narrow inbound allowances, then `drop`."""
	allow = ""
	if public_allow_ports:
		ports = ", ".join(str(port) for port in public_allow_ports)
		allow = f"\t\ttcp dport {{ {ports} }} accept\n"
	return (
		f"table inet {TABLE} {{\n"
		f"\tchain input {{\n"
		f"\t\ttype filter hook input priority filter; policy accept;\n"
		f'\t\tiifname "{public_interface}" jump public_input\n'
		f"\t}}\n"
		f"\tchain public_input {{\n"
		f"\t\tct state established,related accept\n"
		f"\t\tct state invalid drop\n"
		f"\t\tmeta l4proto {{ icmp, icmpv6 }} accept\n"
		# UDP/wg_port (51820) carries BOTH WireGuard planes into this host: the
		# Central-managed control tunnel (spec/21) AND the host-mesh private-plane fabric
		# (wg-mesh, private-networking-host-mesh.md §C4). Both listen on the same fixed
		# port region-wide, so this one accept covers both — no extra host-mesh rule needed.
		f"\t\tudp dport {wg_port} accept\n"
		f"{allow}"
		f"\t\tdrop\n"
		f"\t}}\n"
		f"}}\n"
	)


def loadable_ruleset(public_interface: str, wg_port: int, public_allow_ports: list[str] | None = None) -> str:
	"""`mgmt_ruleset` prefixed with the idempotent add-delete-add idiom, so `nft -f`
	replaces any existing `atlas_mgmt` table cleanly (an empty add makes the delete
	safe even on first apply)."""
	return f"table inet {TABLE} {{}}\ndelete table inet {TABLE}\n" + mgmt_ruleset(
		public_interface, wg_port, public_allow_ports
	)


# --- host functions ---------------------------------------------------------


def discover_public_interface() -> str:
	"""The default-route (uplink) device — the public interface to lock down. Imported
	lazily to keep this module importable for pure unit tests."""
	from atlas.network_env import default_route_device

	return default_route_device()


def apply(public_interface: str, wg_port: int, public_allow_ports: list[str], revert_seconds: int) -> None:
	"""Load the locked ruleset and ARM the auto-revert. The lockdown is live
	immediately but undoes itself after `revert_seconds` unless `confirm` cancels —
	the lockout-safety guarantee."""
	install_file(
		loadable_ruleset(public_interface, wg_port, public_allow_ports), "/run/atlas-mgmt.nft", mode="0600"
	)
	run("sudo nft -f /run/atlas-mgmt.nft")
	arm_revert(revert_seconds)


def arm_revert(seconds: int) -> None:
	"""Schedule `nft delete table inet atlas_mgmt` to run in `seconds` via a transient
	systemd timer (`--collect` so the unit is GC'd after it fires). Clears any prior
	armed revert first, so a re-apply re-arms cleanly."""
	cancel_revert()
	run(
		"sudo systemd-run --collect {} {} {} nft delete table inet {}",
		f"--on-active={seconds}", f"--unit={REVERT_UNIT}",
		"--description=Atlas management-firewall auto-revert (lockout safety)", TABLE,
	)  # fmt: skip


def cancel_revert() -> None:
	"""Stop and clear the armed revert timer/service (best-effort)."""
	for unit in (f"{REVERT_UNIT}.timer", f"{REVERT_UNIT}.service"):
		run("sudo systemctl stop {}", unit, check=False)
		run("sudo systemctl reset-failed {}", unit, check=False)


def revert() -> None:
	"""Restore open access: cancel the armed timer, delete the live table, and remove
	the persisted ruleset + disable the boot unit so a reboot does not re-lock. The
	rollback path and what the armed timer's effect mirrors."""
	cancel_revert()
	run("sudo nft delete table inet {}", TABLE, check=False)
	run("sudo rm -f {}", PERSIST_PATH, check=False)
	run("sudo systemctl disable {}", PERSIST_UNIT, check=False)


def persist(public_interface: str, wg_port: int, public_allow_ports: list[str]) -> None:
	"""Confirm the lockdown: cancel the auto-revert and make the locked ruleset the
	boot default (write the persisted include + enable the fail-closed boot unit). The
	live table is already loaded by `apply`; this just makes it survive a reboot."""
	cancel_revert()
	install_directory("/etc/atlas", mode="0755")
	install_file(loadable_ruleset(public_interface, wg_port, public_allow_ports), PERSIST_PATH, mode="0644")
	run("sudo systemctl enable {}", PERSIST_UNIT, check=False)
