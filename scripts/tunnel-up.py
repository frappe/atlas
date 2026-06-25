#!/usr/bin/env python3
# Bring up this Atlas host's WireGuard spoke interface idempotently: generate the
# spoke keypair locally if absent (private key never leaves the host, 0600), write
# wg0.conf with the assigned /32 and the Central hub as the one peer, wg-quick up,
# enable wg-quick@wg0. Emit this host's PUBLIC key + listen port + tunnel ip as the
# typed ATLAS_RESULT= line that the central_link.provision_tunnel API returns to
# Central (so the hub can add this spoke as a peer).
#
# Runs on the ATLAS controller host (it IS the spoke), invoked through the local
# runner (atlas.atlas.local_task.run_local_task), like issue-cert.py. Privileged
# commands (wg, wg-quick, systemctl) are sudoers-pinned (scripts/sudoers.d/
# atlas-tunnel). Idempotent: safe to re-run / re-provision.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import atlas.tunnel as tunnel
from atlas._task import TaskInputs, TaskResult


@dataclass(frozen=True)
class TunnelUpInputs(TaskInputs):
	"""Bring up the Atlas spoke wg0 toward the Central hub."""

	command: typing.ClassVar[str] = "tunnel-up"
	private_key_path: str  # 0600 path to the spoke private key (generated here if absent)
	tunnel_ip: str  # this Atlas's assigned tunnel address, e.g. 10.88.0.2
	tunnel_cidr: str  # the tunnel pool, used as the hub peer's allowed-ips, e.g. 10.88.0.0/16
	hub_public_key: str  # the Central hub's WireGuard public key
	hub_endpoint: str  # the hub's public ip:port
	listen_port: int = 51820
	interface: str = "wg0"
	keepalive: int = 25


@dataclass(frozen=True)
class TunnelUpResult(TaskResult):
	wg_public_key: str
	listen_port: int
	tunnel_ip: str
	interface: str


def main() -> None:
	inputs = TunnelUpInputs.from_args()

	public_key = tunnel.ensure_keypair(inputs.private_key_path)
	address = inputs.tunnel_ip.split("/")[0] + "/32"
	tunnel.ensure_interface(
		inputs.interface,
		inputs.private_key_path,
		address,
		inputs.listen_port,
		inputs.hub_public_key,
		inputs.hub_endpoint,
		inputs.tunnel_cidr,
		inputs.keepalive,
	)

	TunnelUpResult(
		wg_public_key=public_key,
		listen_port=inputs.listen_port,
		tunnel_ip=inputs.tunnel_ip,
		interface=inputs.interface,
	).emit()
	print(f"Spoke {inputs.interface} up at {address}, peer hub {inputs.hub_endpoint}.")


if __name__ == "__main__":
	main()
