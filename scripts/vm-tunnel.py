#!/usr/bin/env python3
# Bring a WireGuard tunnel up or down on the host for one VM, with no reboot — the
# live half of the VPN broker (spec/19-vpn-broker.md), the tunnel analog of
# vm-reserved-ip.py.
#
# up:   mint the host keypair (idempotent — an existing key is reused, so a
#       re-apply never rotates the key out from under a client that already holds
#       the config), write the <tunnel>.env + <tunnel>.key sidecars under the VM's
#       tunnels/ dir (durable; re-applied at cold boot by vm-network-up.py), apply
#       the live wg interface + the nft isolation rules, and emit the host PUBLIC
#       key (the private half never leaves the host).
# down: remove the live wg interface + its nft rules and delete the sidecars.
#
# A Task (typed --flags), dispatched by VPN Tunnel.request()/revoke(). Imports the
# per-task staged atlas package under /tmp/atlas/lib, like the other Task scripts.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import install_directory, install_file, run, run_input
from atlas._task import TaskInputs, TaskResult
from atlas.network_env import read_network_env
from atlas.paths import VirtualMachinePaths
from atlas.wireguard import TunnelConfig, apply_tunnel, remove_tunnel


@dataclass(frozen=True)
class TunnelInputs(TaskInputs):
	"""Bring a VM's WireGuard tunnel up or down on the host."""

	command: typing.ClassVar[str] = "vm-tunnel"
	tunnel_name: str  # the VPN Tunnel UUID — names the .env / .key sidecars
	virtual_machine_name: str  # the VM UUID — locates the VM dir + network.env
	interface: str  # wg-<id>, derived controller-side from tunnel_name
	action: str = "up"  # "up" | "down"
	# up-only parameters (ignored on down, which works from the interface name):
	listen_port: int = 0
	client_public_key: str = ""
	client_address: str = ""  # the client's overlay /128 (bare address)
	host_address: str = ""  # the host end's /127 overlay CIDR


@dataclass(frozen=True)
class TunnelResult(TaskResult):
	server_public_key: str  # empty on down


def main() -> None:
	inputs = TunnelInputs.from_args()
	if inputs.action not in ("up", "down"):
		sys.exit(f"action must be up|down, got {inputs.action!r}")

	paths = VirtualMachinePaths(inputs.virtual_machine_name)
	key_path = paths.tunnel_key(inputs.tunnel_name)
	env_path = paths.tunnel_env(inputs.tunnel_name)

	if inputs.action == "down":
		remove_tunnel(inputs.interface)
		# Best-effort — a revoke may run after terminate already rm -rf'd the dir.
		for path in (env_path, key_path):
			run("sudo rm -f {}", path, check=False)
		print(f"Tunnel {inputs.interface} down on {inputs.virtual_machine_name}.")
		TunnelResult(server_public_key="").emit()
		return

	virtual_machine_ipv6 = read_network_env(paths.network_env).require("VIRTUAL_MACHINE_IPV6")
	install_directory(paths.tunnels_directory, mode="0700")
	public_key = _ensure_host_key(key_path)
	config = TunnelConfig(
		interface=inputs.interface,
		listen_port=inputs.listen_port,
		private_key_path=key_path,
		client_public_key=inputs.client_public_key,
		client_address=inputs.client_address,
		host_address=inputs.host_address,
		virtual_machine_ipv6=virtual_machine_ipv6,
	)
	# The .env sidecar (0644) carries the durable metadata; the .key (0600) holds
	# the private key. vm-network-up.py re-applies both at cold boot.
	install_file(config.to_env_text(), env_path, mode="0644")
	apply_tunnel(config)
	print(f"Tunnel {inputs.interface} up on {inputs.virtual_machine_name} (port {inputs.listen_port}).")
	TunnelResult(server_public_key=public_key).emit()


def _ensure_host_key(key_path: str) -> str:
	"""Return the tunnel's host PUBLIC key, minting the private key on first use.
	Idempotent: an existing key file is reused so a re-apply (boot reconcile, retry)
	never rotates the key — the client's config stays valid. `wg pubkey` derives the
	public half from the private; neither needs root."""
	if os.path.isfile(key_path):
		# nosemgrep: frappe-security-file-traversal -- host script; reads a per-tunnel key path derived from the tunnel name, not untrusted web input
		with open(key_path) as handle:
			private_key = handle.read().strip()
	else:
		private_key = run("wg genkey").strip()
		install_file(private_key + "\n", key_path, mode="0600")
	return run_input("wg pubkey", stdin=private_key + "\n").strip()


if __name__ == "__main__":
	main()
