import asyncio
import importlib
import sys

import bcrypt
import pytest
from fastapi.testclient import TestClient

PASSWORD = "atlas-password"
KEY_A = "A" * 43 + "="
KEY_B = "B" * 43 + "="


@pytest.fixture
def client(tmp_path, monkeypatch):
	"""Serve the API on one node without a cluster, and record wg setconf instead of running it."""
	path = tmp_path / "wireguard-gateway.toml"
	path.write_text(
		f"""[gateway]
region_id = 1
node_id = "wireguard-001"
private_key = "node-private-key"

[[nodes]]
node_id = "wireguard-001"
gateway_id = 1
endpoint = "wireguard-001.par-1.example.com"
listen_port = 51820
public_key = "node-1-public"

[[nodes]]
node_id = "wireguard-002"
gateway_id = 2
endpoint = "wireguard-002.par-1.example.com"
listen_port = 51820
public_key = "node-2-public"

[auth]
password_hash = "{bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()}"

[cluster]
node_id = "wireguard-001"
password = "cluster-password"
state_path = "{tmp_path / "cluster-state.json"}"
peers = [{{ node_id = "wireguard-001", address = "https://wireguard-001.par-1.example.com" }}]

[tls]
fullchain_pem = "leaf"
private_key_pem = "key"
"""
	)
	monkeypatch.setenv("ATLAS_WG_GATEWAY_CONFIG", str(path))
	sys.modules.pop("gatewayd.main", None)
	main = importlib.import_module("gatewayd.main")
	monkeypatch.setattr(main.peer_state, "apply_wireguard", lambda state: None)
	monkeypatch.setattr(main.peer_state, "is_serving", lambda: True)
	asyncio.run(main.cluster.start())
	return TestClient(main.app, headers={"Authorization": f"Bearer {PASSWORD}"})


def test_a_registration_is_repeatable_and_listed_with_its_node(client):
	first = client.post("/v1/peers", json={"tenant_id": 42, "client_id": 9, "public_key": KEY_A}).json()
	again = client.post("/v1/peers", json={"tenant_id": 42, "client_id": 9, "public_key": KEY_A}).json()

	assert first == again
	assert client.get("/v1/peers").json() == [first | {"node_id": "wireguard-001"}]


def test_a_restore_replaces_the_table_and_keeps_the_nodes(client):
	client.post("/v1/peers", json={"tenant_id": 1, "client_id": 1, "public_key": KEY_A})

	response = client.put(
		"/v1/peers",
		json={"peers": [{"tenant_id": 7, "client_id": 3, "public_key": KEY_B, "node_id": "wireguard-002"}]},
	)

	assert response.json() == {"peers": 1}
	assert [(peer["tenant_id"], peer["node_id"]) for peer in client.get("/v1/peers").json()] == [
		(7, "wireguard-002")
	]


def test_a_caller_without_credentials_is_refused(client):
	assert client.get("/v1/peers", headers={"Authorization": ""}).status_code == 401


def test_health_needs_an_applied_wireguard_interface(client, monkeypatch):
	main = sys.modules["gatewayd.main"]
	monkeypatch.setattr(main.cluster, "is_initialized", True)

	monkeypatch.setattr(main.peer_state, "is_interface_ready", lambda: False)
	assert client.get("/healthz").status_code == 503

	monkeypatch.setattr(main.peer_state, "is_interface_ready", lambda: True)
	assert client.get("/healthz").status_code == 204


def test_a_repeated_registration_goes_through_the_cluster(client, monkeypatch):
	main = sys.modules["gatewayd.main"]
	body = {"tenant_id": 42, "client_id": 9, "public_key": KEY_A}
	client.post("/v1/peers", json=body)
	calls = []
	mutate = main.cluster.mutate

	async def counting_mutate(mutation):
		calls.append(mutation)
		return await mutate(mutation)

	monkeypatch.setattr(main.cluster, "mutate", counting_mutate)
	response = client.post("/v1/peers", json=body).json()

	assert len(calls) == 1
	assert response["endpoint"] == "wireguard-001.par-1.example.com:51820"
