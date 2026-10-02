import ipaddress
import os
import re
import subprocess
import time
from collections import Counter
from typing import Any

from atlas_control.cluster import Mutation
from fastapi import HTTPException

from .config import GatewayConfig, Node

PEERS_KIND = "peers"
CLIENT_PREFIX = 0xFDAC
MESH_PREFIX = 0xFDAA
FIELD_LIMIT = (1 << 32) - 1
PUBLIC_KEY_PATTERN = re.compile(r"[A-Za-z0-9+/]{43}=")
NODE_ID_PATTERN = re.compile(r"wireguard-[0-9]{3,5}")
INTERFACE = "wg0"
SERVING_CHECK_SECONDS = 1.0


def get_identity(tenant_id: int, client_id: int) -> str:
	"""Return the peer table key of one customer device."""
	return f"{tenant_id}:{client_id}"


def validate_device(tenant_id: int, client_id: int, public_key: str) -> None:
	"""Reject a device that the address layout or WireGuard cannot hold."""
	for label, value in (("tenant_id", tenant_id), ("client_id", client_id)):
		if not 1 <= value <= FIELD_LIMIT:
			raise HTTPException(status_code=400, detail=f"{label} must be from 1 to {FIELD_LIMIT}")
	if not PUBLIC_KEY_PATTERN.fullmatch(public_key):
		raise HTTPException(status_code=400, detail="public_key must be a 44-character WireGuard key")


class PeerState:
	"""Replicate every customer device of the region, and serve the devices of this node."""

	def __init__(self, config: GatewayConfig):
		self.config = config
		self.nodes = {node.node_id: node for node in config.nodes}
		self.serving = False
		self.serving_checked_at = float("-inf")

	def is_serving(self) -> bool:
		"""Report whether wg0 serves devices. Heartbeats ask often, so one answer lasts a second."""
		now = time.monotonic()
		if now - self.serving_checked_at >= SERVING_CHECK_SECONDS:
			self.serving = self.is_interface_ready()
			self.serving_checked_at = now
		return self.serving

	async def initial_state(self) -> dict[str, Any]:
		return {PEERS_KIND: {}}

	async def restore(self, state: dict[str, Any]) -> None:
		self.apply_wireguard(state)

	async def prepare(
		self, state: dict[str, Any], mutation: Mutation, serving_members: frozenset[str]
	) -> Mutation:
		"""Place each device without a node on the serving node with the fewest devices. Runs on the leader."""
		if mutation.action == "update" and isinstance(mutation.value, dict):
			current = state.get(PEERS_KIND, {}).get(mutation.key)
			node_id = current["node_id"] if current else self.assign_node(state, serving_members).node_id
			return mutation.model_copy(update={"value": {**mutation.value, "node_id": node_id}})

		if mutation.action == "replace" and isinstance(mutation.value, dict):
			table: dict[str, dict[str, str]] = {}
			for identity, peer in mutation.value.items():
				node_id = (
					peer.get("node_id") or self.assign_node({PEERS_KIND: table}, serving_members).node_id
				)
				table[identity] = {**peer, "node_id": node_id}
			return mutation.model_copy(update={"value": table})
		return mutation

	async def apply(self, state: dict[str, Any], mutation: Mutation) -> dict[str, object]:
		"""Apply the changed table to wg0 first. The stored table changes only after wg setconf succeeds."""
		if mutation.kind != PEERS_KIND:
			raise HTTPException(status_code=400, detail=f"unknown state kind {mutation.kind}")

		current: dict[str, dict[str, str]] = state.get(PEERS_KIND, {})
		peers = self.get_changed_table(current, mutation)
		if self.has_node_changes(current, peers):
			self.apply_wireguard({PEERS_KIND: peers})
		state[PEERS_KIND] = peers

		if mutation.action == "replace":
			return {"peers": len(peers)}
		if mutation.action == "delete":
			return {}
		return self.get_credentials(mutation.key, peers[mutation.key])

	def get_changed_table(
		self, current: dict[str, dict[str, str]], mutation: Mutation
	) -> dict[str, dict[str, str]]:
		"""Return a new table with one mutation applied. The current table stays unchanged."""
		if mutation.action == "replace":
			return self.validate_table(mutation.value)

		peers = {identity: dict(peer) for identity, peer in current.items()}
		if mutation.action == "delete":
			peers.pop(mutation.key, None)
		else:
			peers[mutation.key] = self.validate_peer(peers, mutation.key, mutation.value)
		return peers

	def has_node_changes(self, current: dict[str, dict[str, str]], peers: dict[str, dict[str, str]]) -> bool:
		"""Report whether the devices of this node differ between two tables."""

		def own(table: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
			return {
				identity: peer for identity, peer in table.items() if peer["node_id"] == self.config.node_id
			}

		return own(current) != own(peers)

	def validate_peer(
		self,
		peers: dict[str, dict[str, str]],
		identity: str,
		value: object,
		is_current_node_required: bool = True,
	) -> dict[str, str]:
		"""Return one new peer, or refuse a key or node that conflicts with the table.

		A restored table can hold devices of an archived node until an operator removes them."""
		if not isinstance(value, dict) or not NODE_ID_PATTERN.fullmatch(str(value.get("node_id", ""))):
			raise HTTPException(status_code=400, detail="a peer needs a public_key and a node_id")
		if is_current_node_required and value["node_id"] not in self.nodes:
			raise HTTPException(status_code=400, detail=f"node {value['node_id']} is not a cluster member")
		peer = {"public_key": str(value.get("public_key", "")), "node_id": str(value["node_id"])}

		current = peers.get(identity)
		if current and current["public_key"] != peer["public_key"]:
			raise HTTPException(status_code=409, detail="this device is registered with another public key")
		if any(
			other != identity and item["public_key"] == peer["public_key"] for other, item in peers.items()
		):
			raise HTTPException(status_code=409, detail="this public key belongs to another device")
		return current or peer

	def validate_table(self, value: object) -> dict[str, dict[str, str]]:
		"""Return a complete device table, or refuse it when a device or public key repeats."""
		if not isinstance(value, dict):
			raise HTTPException(status_code=400, detail="a replacement needs the complete device table")
		peers: dict[str, dict[str, str]] = {}
		for identity, peer in value.items():
			peers[identity] = self.validate_peer(peers, identity, peer, is_current_node_required=False)
		return peers

	@staticmethod
	def build_table(devices: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
		"""Return the device table for a restore. The leader places a device without a node."""
		table: dict[str, dict[str, str]] = {}
		for device in devices:
			validate_device(device["tenant_id"], device["client_id"], device["public_key"])
			identity = get_identity(device["tenant_id"], device["client_id"])
			if identity in table:
				raise HTTPException(status_code=400, detail=f"device {identity} appears twice")
			table[identity] = {"public_key": device["public_key"], "node_id": device.get("node_id") or ""}
		return table

	def assign_node(self, state: dict[str, Any], serving_members: frozenset[str]) -> Node:
		"""Return the serving node with the fewest devices. The node ID breaks a tie."""
		counts = Counter(peer["node_id"] for peer in state.get(PEERS_KIND, {}).values())
		candidates = [node for node in self.nodes.values() if node.node_id in serving_members]
		if not candidates:
			raise HTTPException(status_code=503, detail="no gateway node is serving")
		return min(candidates, key=lambda node: (counts[node.node_id], node.node_id))

	def get_credentials(self, identity: str, peer: dict[str, str]) -> dict[str, object]:
		"""Return the WireGuard settings that the device needs. A device of an archived node has no endpoint."""
		tenant_id, client_id = (int(value) for value in identity.split(":"))
		node = self.nodes.get(peer["node_id"])
		# The node number is part of the record name, so an archived node still gives the address.
		gateway_id = int(peer["node_id"].rsplit("-", 1)[-1])
		region = self.config.region_id
		address = (
			(CLIENT_PREFIX << 112)
			| (region << 96)
			| (gateway_id << 80)
			| (tenant_id << 48)
			| (client_id << 16)
		)
		tenant_network = (MESH_PREFIX << 112) | (region << 96) | (tenant_id << 64)
		return {
			"tenant_id": tenant_id,
			"client_id": client_id,
			"address": f"{ipaddress.IPv6Address(address)}/128",
			"allowed_ips": [str(ipaddress.IPv6Network((tenant_network, 64)))],
			"endpoint": f"{node.endpoint}:{node.listen_port}" if node else "",
			"public_key": node.public_key if node else "",
		}

	def render_wireguard(self, state: dict[str, Any]) -> str:
		"""Return the wg setconf file with the devices of this node."""
		lines = [
			"[Interface]",
			f"PrivateKey = {self.config.private_key}",
			f"ListenPort = {self.config.node.listen_port}",
		]
		for identity, peer in sorted(state.get(PEERS_KIND, {}).items()):
			if peer["node_id"] != self.config.node_id:
				continue
			address = self.get_credentials(identity, peer)["address"]
			lines += ["", "[Peer]", f"PublicKey = {peer['public_key']}", f"AllowedIPs = {address}"]
		return "\n".join(lines) + "\n"

	@staticmethod
	def is_interface_ready() -> bool:
		"""Report whether wg0 holds an applied configuration. Only an applied file sets the listen port."""
		result = subprocess.run(["wg", "show", INTERFACE, "listen-port"], capture_output=True, text=True)
		return result.returncode == 0 and result.stdout.strip() not in ("", "0")

	def apply_wireguard(self, state: dict[str, Any]) -> None:
		"""Replace the WireGuard configuration of this node."""
		path = self.config.state_directory / "wg0.conf"
		descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
		with os.fdopen(descriptor, "w") as target:
			target.write(self.render_wireguard(state))
		try:
			subprocess.run(
				["wg", "setconf", INTERFACE, str(path)], check=True, capture_output=True, text=True
			)
		except subprocess.CalledProcessError as error:
			raise HTTPException(
				status_code=502, detail=f"wg setconf failed: {error.stderr.strip()}"
			) from error
