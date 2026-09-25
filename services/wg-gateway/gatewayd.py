#!/usr/bin/env python3
"""Standalone WireGuard gateway API. Central calls this daemon through the HTTP proxy."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import subprocess
import threading
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

FDAC_PREFIX = 0xFDAC
MESH_PREFIX = 0xFDAA
REGION_LIMIT = 1 << 16
TENANT_LIMIT = 1 << 32
CLIENT_LIMIT = 1 << 32
PUBLIC_KEY_PATTERN = re.compile(r"[A-Za-z0-9+/]{43}=")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_apply_lock = threading.Lock()


def _config() -> dict[str, Any]:
	"""Read the daemon configuration written by setup.sh."""
	return {
		"token": os.environ["WG_GATEWAY_TOKEN"],
		"bind": os.environ["WG_GATEWAY_BIND"],
		"port": int(os.environ.get("WG_GATEWAY_PORT", "8080")),
		"region": int(os.environ["WG_GATEWAY_REGION"]),
		"listen_port": int(os.environ["WG_GATEWAY_LISTEN_PORT"]),
		"state_dir": os.environ.get("WG_GATEWAY_STATE_DIR", "/opt/atlas/wg-gateway"),
		"interface": os.environ.get("WG_GATEWAY_INTERFACE", "wg0"),
	}


def _authorize(authorization: str | None, token: str) -> None:
	"""Reject any request without the bearer token."""
	if not authorization or not secrets.compare_digest(authorization, f"Bearer {token}"):
		raise HTTPException(status_code=401, detail="Unauthorized")


def _checked_int(label: str, value: object, low: int, high: int) -> int:
	"""Return value as an int inside its field, or reject the request."""
	if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
		raise HTTPException(status_code=400, detail=f"{label} must be an integer from {low} to {high}.")
	return value


def _path_int(label: str, value: str, high: int) -> int:
	"""Return a URL path value as an int inside its field, or reject the request."""
	try:
		number = int(value, 10)
	except (TypeError, ValueError):
		raise HTTPException(status_code=400, detail=f"{label} must be an integer.")
	return _checked_int(label, number, 1, high)


def _checked_key(value: object) -> str:
	"""Return the key or reject anything that is not a WireGuard public key."""
	if not isinstance(value, str) or not PUBLIC_KEY_PATTERN.fullmatch(value.strip()):
		raise HTTPException(
			status_code=400, detail="Public key must be a 44-character base64 WireGuard public key."
		)
	return value.strip()


def _client_fdac(region: int, tenant: int, client: int) -> str:
	"""Return the fdac address of one WireGuard client."""
	address = (FDAC_PREFIX << 112) | (region << 96) | (tenant << 64) | client
	return str(ipaddress.IPv6Address(address))


def _tenant_prefix(region: int, tenant: int) -> str:
	"""Return the /64 of one tenant's private VM addresses."""
	network = ipaddress.IPv6Network(((MESH_PREFIX << 112) | (region << 96) | (tenant << 64), 64))
	return str(network)


def _peers_path(config: dict[str, Any]) -> str:
	"""Return the stored peer list path."""
	return os.path.join(config["state_dir"], "peers.json")


def _load_peers(config: dict[str, Any]) -> list[dict[str, Any]]:
	"""Return the stored peers, or an empty list on a fresh gateway."""
	try:
		with open(_peers_path(config), encoding="utf-8") as handle:
			data = json.load(handle)
	except FileNotFoundError:
		return []
	if not isinstance(data, dict) or not isinstance(data.get("peers"), list):
		raise HTTPException(status_code=500, detail="The stored peer list is not readable.")
	return data["peers"]


def _store_peers(config: dict[str, Any], peers: list[dict[str, Any]]) -> None:
	"""Store the peer list with owner-only access."""
	path = _peers_path(config)
	mode = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(mode, "w", encoding="utf-8") as handle:
		json.dump({"peers": peers}, handle, indent=2)
	os.chmod(path, 0o600)


def _render_wireguard_conf(config: dict[str, Any], peers: list[dict[str, Any]]) -> str:
	"""Return the wg setconf content with the interface section first."""
	with open(os.path.join(config["state_dir"], "privatekey"), encoding="utf-8") as handle:
		private_key = handle.read().strip()
	lines = ["[Interface]", f"PrivateKey = {private_key}", f"ListenPort = {config['listen_port']}", ""]
	for peer in peers:
		lines += [
			"[Peer]",
			f"PublicKey = {peer['public_key']}",
			f"AllowedIPs = {peer['fdac']}/128",
			"",
		]
	return "\n".join(lines) + "\n"


def _render_nft(config: dict[str, Any], peers: list[dict[str, Any]]) -> str:
	"""Return the atlas_wg_gateway table with one tenant-wide rule per tenant."""
	by_tenant: dict[int, list[str]] = {}
	for peer in peers:
		by_tenant.setdefault(peer["tenant_id"], []).append(peer["fdac"])
	rules = ["\t\tct state established,related counter accept"]
	for tenant_id in sorted(by_tenant):
		sources = ", ".join(sorted(by_tenant[tenant_id]))
		rules.append(
			f"\t\tip6 saddr {{ {sources} }} ip6 daddr {_tenant_prefix(config['region'], tenant_id)}"
			" ct state new counter accept"
		)
	accepts = "\n".join(rules)
	return f"""table ip6 atlas_wg_gateway {{}}
delete table ip6 atlas_wg_gateway

table ip6 atlas_wg_gateway {{
\tchain forward {{
\t\ttype filter hook forward priority filter; policy drop;
{accepts}
\t}}

\tchain postrouting {{
\t\ttype nat hook postrouting priority srcnat; policy accept;
\t\tip6 saddr fdac::/16 ip6 daddr fdaa::/16 counter snat to {config["bind"]}
\t}}

\t# The gateway forwards out of the interface that received the packet. A redirect would send the host around it.
\tchain output {{
\t\ttype filter hook output priority filter; policy accept;
\t\ticmpv6 type nd-redirect drop
\t}}
}}
"""


def _write_private(path: str, content: str) -> None:
	"""Write a secret file with owner-only access."""
	mode = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(mode, "w", encoding="utf-8") as handle:
		handle.write(content)
	os.chmod(path, 0o600)


def _apply(config: dict[str, Any], peers: list[dict[str, Any]]) -> None:
	"""Replace the WireGuard peers and firewall with the desired state."""
	state_dir = config["state_dir"]
	peers_conf = os.path.join(state_dir, "peers.conf")
	nft_path = os.path.join(state_dir, "gateway.nft")
	_write_private(peers_conf, _render_wireguard_conf(config, peers))
	_write_private(nft_path, _render_nft(config, peers))
	try:
		subprocess.run(
			["wg", "setconf", config["interface"], peers_conf],
			check=True,
			capture_output=True,
			text=True,
		)
		subprocess.run(["nft", "-f", nft_path], check=True, capture_output=True, text=True)
	except subprocess.CalledProcessError as error:
		raise HTTPException(
			status_code=502,
			detail=f"The gateway rejected the update: {(error.stderr or '').strip() or error}",
		)


def _validated_peers(config: dict[str, Any], peers: object) -> list[dict[str, Any]]:
	"""Return the peer records with fdac addresses, or reject the list."""
	if not isinstance(peers, list):
		raise HTTPException(
			status_code=400, detail="Peers must be a list of tenant, client, and public key objects."
		)
	wanted: list[dict[str, Any]] = []
	seen: set[tuple[int, int]] = set()
	seen_keys: set[str] = set()
	for index, peer in enumerate(peers):
		if not isinstance(peer, dict):
			raise HTTPException(status_code=400, detail=f"Peer {index} must be an object.")
		key = _checked_key(peer.get("public_key"))
		tenant = _checked_int("Tenant ID", peer.get("tenant_id"), 1, TENANT_LIMIT - 1)
		client = _checked_int("Client ID", peer.get("client_id"), 1, CLIENT_LIMIT - 1)
		if (tenant, client) in seen:
			raise HTTPException(status_code=400, detail=f"Client {client} of tenant {tenant} appears twice.")
		seen.add((tenant, client))
		if key in seen_keys:
			raise HTTPException(status_code=400, detail="One public key appears for two clients.")
		seen_keys.add(key)
		wanted.append(
			{
				"tenant_id": tenant,
				"client_id": client,
				"public_key": key,
				"fdac": _client_fdac(config["region"], tenant, client),
			}
		)
	return wanted


@app.get("/healthz")
def health(authorization: str | None = Header(default=None)) -> dict[str, str]:
	"""Report that the daemon answers."""
	_authorize(authorization, _config()["token"])
	return {"status": "ok"}


@app.get("/config")
def read_config(authorization: str | None = Header(default=None)) -> dict[str, Any]:
	"""Return the connection values a customer needs for this gateway."""
	config = _config()
	_authorize(authorization, config["token"])
	with open(os.path.join(config["state_dir"], "publickey"), encoding="utf-8") as handle:
		public_key = handle.read().strip()
	return {
		"public_key": public_key,
		"listen_port": config["listen_port"],
		"region_id": config["region"],
		"mesh": config["bind"],
	}


@app.get("/peers")
def list_peers(authorization: str | None = Header(default=None)) -> dict[str, Any]:
	"""Return the peers of this gateway."""
	config = _config()
	_authorize(authorization, config["token"])
	return {"peers": _load_peers(config)}


@app.put("/peers")
async def replace_peers(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
	"""Replace the complete peer list of this gateway."""
	config = _config()
	_authorize(authorization, config["token"])
	try:
		body = await request.json()
	except ValueError:
		raise HTTPException(
			status_code=400, detail="Peers must be a list of tenant, client, and public key objects."
		)
	wanted = _validated_peers(config, body.get("peers") if isinstance(body, dict) else None)
	with _apply_lock:
		current = {(peer["tenant_id"], peer["client_id"]): peer for peer in _load_peers(config)}
		wanted_map = {(peer["tenant_id"], peer["client_id"]): peer for peer in wanted}
		added = len([identity for identity in wanted_map if identity not in current])
		removed = len([identity for identity in current if identity not in wanted_map])
		updated = len(
			[
				identity
				for identity in wanted_map
				if identity in current
				and current[identity]["public_key"] != wanted_map[identity]["public_key"]
			]
		)
		_store_peers(config, wanted)
		_apply(config, wanted)
	return {"peers": wanted, "added": added, "removed": removed, "updated": updated}


@app.put("/peers/{tenant_id}/{client_id}")
async def upsert_peer(
	tenant_id: str, client_id: str, request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
	"""Add one client to this gateway and sync it."""
	config = _config()
	_authorize(authorization, config["token"])
	tenant = _path_int("Tenant ID", tenant_id, TENANT_LIMIT - 1)
	client = _path_int("Client ID", client_id, CLIENT_LIMIT - 1)
	try:
		body = await request.json()
	except ValueError:
		raise HTTPException(status_code=400, detail="The request must carry a public key object.")
	key = _checked_key(body.get("public_key") if isinstance(body, dict) else None)
	with _apply_lock:
		peers = _load_peers(config)
		for peer in peers:
			if (peer["tenant_id"], peer["client_id"]) == (tenant, client):
				if peer["public_key"] != key:
					raise HTTPException(
						status_code=400,
						detail=f"Client {client} of tenant {tenant} already uses another public key.",
					)
				_apply(config, peers)
				return peer
			if peer["public_key"] == key:
				raise HTTPException(
					status_code=400, detail="This public key is already a peer of this gateway."
				)
		record = {
			"tenant_id": tenant,
			"client_id": client,
			"public_key": key,
			"fdac": _client_fdac(config["region"], tenant, client),
		}
		peers.append(record)
		_store_peers(config, peers)
		_apply(config, peers)
		return record


@app.delete("/peers/{tenant_id}/{client_id}")
def delete_peer(
	tenant_id: str, client_id: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
	"""Delete one client of this gateway and sync it. Missing peers are gone."""
	config = _config()
	_authorize(authorization, config["token"])
	tenant = _path_int("Tenant ID", tenant_id, TENANT_LIMIT - 1)
	client = _path_int("Client ID", client_id, CLIENT_LIMIT - 1)
	with _apply_lock:
		peers = [
			peer for peer in _load_peers(config) if (peer["tenant_id"], peer["client_id"]) != (tenant, client)
		]
		_store_peers(config, peers)
		_apply(config, peers)
		return {"gone": True}
