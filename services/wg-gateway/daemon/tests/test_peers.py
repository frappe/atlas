import asyncio
from pathlib import Path

import pytest
from atlas_control.cluster import Mutation
from atlas_control.config import AuthConfig, ClusterConfig
from fastapi import HTTPException

from gatewayd.config import ConfigError, GatewayConfig, Node, load
from gatewayd.peers import PEERS_KIND, PeerState, validate_device

KEY_A = "A" * 43 + "="
KEY_B = "B" * 43 + "="
BOTH_NODES = frozenset({"wireguard-001", "wireguard-007"})


def gateway_config(tmp_path: Path, node_id: str = "wireguard-001") -> GatewayConfig:
	return GatewayConfig(
		region_id=2,
		node_id=node_id,
		private_key="node-private-key",
		nodes=(
			Node("wireguard-001", 1, "wireguard-001.par-1.example.com", 51820, "node-1-public"),
			Node("wireguard-007", 7, "wireguard-007.par-1.example.com", 51821, "node-7-public"),
		),
		auth=AuthConfig(),
		cluster=ClusterConfig(state_path=tmp_path / "cluster-state.json"),
		certificate_pem="leaf",
		private_key_pem="key",
		state_directory=tmp_path,
	)


class RecordingPeerState(PeerState):
	"""Record wg setconf content instead of running it."""

	def apply_wireguard(self, state):
		self.applied = self.render_wireguard(state)


def register(peers: PeerState, state: dict, identity: str, public_key: str, node_id: str) -> dict:
	mutation = Mutation(
		kind=PEERS_KIND, action="update", key=identity, value={"public_key": public_key, "node_id": node_id}
	)
	return asyncio.run(peers.apply(state, mutation))


def test_credentials_carry_the_node_tenant_and_client_in_the_address(tmp_path):
	peers = RecordingPeerState(gateway_config(tmp_path))

	credentials = register(peers, {}, "42:9", KEY_A, "wireguard-007")

	assert credentials["address"] == "fdac:2:7:0:2a::9/128"
	assert credentials["allowed_ips"] == ["fdaa:2:0:2a::/64"]
	assert credentials["endpoint"] == "wireguard-007.par-1.example.com:51821"
	assert credentials["public_key"] == "node-7-public"


def test_a_new_device_goes_to_the_node_with_the_fewest_devices(tmp_path):
	peers = RecordingPeerState(gateway_config(tmp_path))
	state = {}

	register(peers, state, "1:1", KEY_A, peers.assign_node(state, BOTH_NODES).node_id)

	assert peers.assign_node(state, BOTH_NODES).node_id == "wireguard-007"


def test_a_new_device_never_goes_to_a_node_that_is_down(tmp_path):
	peers = RecordingPeerState(gateway_config(tmp_path))
	state = {PEERS_KIND: {"1:1": {"public_key": KEY_A, "node_id": "wireguard-001"}}}
	mutation = Mutation(kind=PEERS_KIND, action="update", key="1:2", value={"public_key": KEY_B})

	prepared = asyncio.run(peers.prepare(state, mutation, frozenset({"wireguard-001"})))

	assert prepared.value["node_id"] == "wireguard-001"


def test_no_serving_node_refuses_a_new_device(tmp_path):
	peers = RecordingPeerState(gateway_config(tmp_path))
	mutation = Mutation(kind=PEERS_KIND, action="update", key="1:1", value={"public_key": KEY_A})

	with pytest.raises(HTTPException) as error:
		asyncio.run(peers.prepare({}, mutation, frozenset()))
	assert error.value.status_code == 503


def test_a_registered_device_keeps_its_node_when_it_registers_again(tmp_path):
	peers = RecordingPeerState(gateway_config(tmp_path))
	state = {PEERS_KIND: {"1:1": {"public_key": KEY_A, "node_id": "wireguard-007"}}}
	mutation = Mutation(kind=PEERS_KIND, action="update", key="1:1", value={"public_key": KEY_A})

	prepared = asyncio.run(peers.prepare(state, mutation, frozenset({"wireguard-001"})))

	assert prepared.value["node_id"] == "wireguard-007"


def test_only_this_node_applies_its_devices(tmp_path):
	peers = RecordingPeerState(gateway_config(tmp_path, "wireguard-001"))
	state = {}

	register(peers, state, "1:1", KEY_A, "wireguard-001")
	register(peers, state, "1:2", KEY_B, "wireguard-007")

	assert peers.applied.count("[Peer]") == 1
	assert f"PublicKey = {KEY_A}" in peers.applied
	assert "AllowedIPs = fdac:2:1:0:1::1/128" in peers.applied


def test_a_repeated_registration_keeps_the_first_node(tmp_path):
	peers = RecordingPeerState(gateway_config(tmp_path))
	state = {}

	register(peers, state, "1:1", KEY_A, "wireguard-001")
	credentials = register(peers, state, "1:1", KEY_A, "wireguard-007")

	assert credentials["endpoint"].startswith("wireguard-001.")


def test_a_conflicting_key_is_refused(tmp_path):
	peers = RecordingPeerState(gateway_config(tmp_path))
	state = {}
	register(peers, state, "1:1", KEY_A, "wireguard-001")

	for identity, public_key in (("1:1", KEY_B), ("1:2", KEY_A)):
		with pytest.raises(HTTPException) as error:
			register(peers, state, identity, public_key, "wireguard-001")
		assert error.value.status_code == 409


def test_an_unknown_node_is_refused(tmp_path):
	peers = RecordingPeerState(gateway_config(tmp_path))

	with pytest.raises(HTTPException):
		register(peers, {}, "1:1", KEY_A, "wireguard-999")


def test_a_restore_keeps_each_device_on_its_node_and_assigns_the_rest(tmp_path):
	peers = RecordingPeerState(gateway_config(tmp_path, "wireguard-007"))
	state = {PEERS_KIND: {"9:9": {"public_key": KEY_B, "node_id": "wireguard-001"}}}
	table = peers.build_table(
		[
			{"tenant_id": 1, "client_id": 1, "public_key": KEY_A, "node_id": "wireguard-007"},
			{"tenant_id": 1, "client_id": 2, "public_key": KEY_B},
		]
	)

	mutation = Mutation(kind=PEERS_KIND, action="replace", value=table)
	asyncio.run(peers.apply(state, asyncio.run(peers.prepare(state, mutation, BOTH_NODES))))

	assert state[PEERS_KIND] == {
		"1:1": {"public_key": KEY_A, "node_id": "wireguard-007"},
		"1:2": {"public_key": KEY_B, "node_id": "wireguard-001"},
	}
	assert peers.applied.count("[Peer]") == 1


def test_a_device_of_an_archived_node_is_kept_without_an_endpoint(tmp_path):
	peers = RecordingPeerState(gateway_config(tmp_path))
	state = {}
	table = {"1:1": {"public_key": KEY_A, "node_id": "wireguard-002"}}

	asyncio.run(peers.apply(state, Mutation(kind=PEERS_KIND, action="replace", value=table)))
	credentials = peers.get_credentials("1:1", state[PEERS_KIND]["1:1"])

	assert credentials["address"] == "fdac:2:2:0:1::1/128"
	assert (credentials["endpoint"], credentials["public_key"]) == ("", "")
	with pytest.raises(HTTPException):
		register(peers, state, "1:2", KEY_B, "wireguard-002")


def test_a_restore_with_a_repeated_key_changes_nothing(tmp_path):
	peers = RecordingPeerState(gateway_config(tmp_path))
	state = {PEERS_KIND: {}}
	table = {
		"1:1": {"public_key": KEY_A, "node_id": "wireguard-001"},
		"1:2": {"public_key": KEY_A, "node_id": "wireguard-007"},
	}

	with pytest.raises(HTTPException):
		asyncio.run(peers.apply(state, Mutation(kind=PEERS_KIND, action="replace", value=table)))
	assert state == {PEERS_KIND: {}}


def test_a_broken_interface_stops_serving(tmp_path, monkeypatch):
	peers = RecordingPeerState(gateway_config(tmp_path))
	monkeypatch.setattr(PeerState, "is_interface_ready", staticmethod(lambda: False))

	assert not peers.is_serving()


def test_a_configuration_without_this_node_is_refused(tmp_path):
	path = tmp_path / "wireguard-gateway.toml"
	path.write_text(
		"""[gateway]
region_id = 2
node_id = "wireguard-003"
private_key = "key"

[[nodes]]
node_id = "wireguard-001"
gateway_id = 1
endpoint = "wireguard-001.par-1.example.com"
listen_port = 51820
public_key = "node-1-public"

[tls]
fullchain_pem = "leaf"
private_key_pem = "key"
"""
	)

	with pytest.raises(ConfigError, match="gateway.node_id"):
		load(path)


def test_a_failed_apply_leaves_the_table_unchanged(tmp_path):
	class FailingPeerState(PeerState):
		def apply_wireguard(self, state):
			raise HTTPException(status_code=502, detail="wg setconf failed")

	peers = FailingPeerState(gateway_config(tmp_path, "wireguard-001"))
	state = {PEERS_KIND: {}}

	with pytest.raises(HTTPException):
		register(peers, state, "1:1", KEY_A, "wireguard-001")
	assert state == {PEERS_KIND: {}}


def test_a_client_number_must_fit_one_hextet():
	validate_device(1, 65535, KEY_A)
	with pytest.raises(HTTPException):
		validate_device(1, 65536, KEY_A)
