#!/usr/bin/env python3
# Apply or clear a VM's public-ingress firewall on the host, with no reboot
# (spec/20-firewall.md) — the live half of the Firewall feature, the firewall
# analog of vm-tunnel.py.
#
# apply: write the firewall.env sidecar (durable; re-applied at cold boot by
#        vm-network-up.py) and install the nft public_filter block restricting
#        the VM's PUBLIC IPv6 to the listed proto/port rules. An empty rule list
#        is a valid deny-all-public (the VM is then reachable only over its VPN).
# clear: remove the nft block and delete the sidecar, reverting the VM to fully
#        public.
#
# A Task (typed --flags), dispatched by Firewall.apply()/clear(). Imports the
# per-task staged atlas package under /tmp/atlas/lib, like the other Task scripts.

import os
import sys
import typing
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import install_file, run
from atlas._task import TaskInputs
from atlas.firewall import FirewallConfig, Rule, apply_firewall, remove_firewall
from atlas.network_env import read_network_env
from atlas.paths import VirtualMachinePaths


@dataclass(frozen=True)
class FirewallInputs(TaskInputs):
	"""Apply or clear a VM's public-ingress firewall on the host."""

	command: typing.ClassVar[str] = "firewall-apply"
	virtual_machine_name: str  # the VM UUID — locates the VM dir + network.env
	action: str = "apply"  # "apply" | "clear"
	# A repeatable flag: --rule tcp/443 --rule udp/1194. Empty on a deny-all-public
	# firewall; ignored on clear.
	rule: list[str] = field(default_factory=list)


def main() -> None:
	inputs = FirewallInputs.from_args()
	if inputs.action not in ("apply", "clear"):
		sys.exit(f"action must be apply|clear, got {inputs.action!r}")

	paths = VirtualMachinePaths(inputs.virtual_machine_name)

	if inputs.action == "clear":
		virtual_machine_ipv6 = read_network_env(paths.network_env).require("VIRTUAL_MACHINE_IPV6")
		remove_firewall(virtual_machine_ipv6)
		# Best-effort — a clear may run after terminate already rm -rf'd the dir.
		run("sudo rm -f {}", paths.firewall_env, check=False)
		print(f"Firewall cleared on {inputs.virtual_machine_name}; VM is fully public.")
		return

	virtual_machine_ipv6 = read_network_env(paths.network_env).require("VIRTUAL_MACHINE_IPV6")
	config = FirewallConfig(
		virtual_machine_ipv6=virtual_machine_ipv6,
		rules=tuple(Rule.parse(token) for token in inputs.rule),
	)
	# The .env sidecar (0644) carries the durable metadata; vm-network-up.py
	# re-applies it at cold boot.
	install_file(config.to_env_text(), paths.firewall_env, mode="0644")
	apply_firewall(config)
	allowed = ", ".join(rule.token() for rule in config.rules) or "(deny all public)"
	print(f"Firewall applied on {inputs.virtual_machine_name}: {allowed}.")


if __name__ == "__main__":
	main()
