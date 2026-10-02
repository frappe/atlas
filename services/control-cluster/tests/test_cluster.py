import asyncio
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from atlas_control.cluster import (
	FORWARDED_WRITE_TIMEOUT_SECONDS,
	PEER_CALL_ATTEMPTS,
	PEER_REPAIR_CALLS,
	PEER_TIMEOUT_SECONDS,
	ClusterManager,
	ClusterSnapshot,
	HeartbeatRequest,
	Mutation,
	SnapshotStore,
	VoteRequest,
)
from atlas_control.config import ClusterConfig, ClusterPeer


class _MemoryState:
	"""Replicate one item map in memory for cluster unit tests."""

	def __init__(self):
		self.values: dict[str, str] = {}

	async def initial_state(self):
		return {"items": dict(self.values)}

	async def restore(self, state):
		self.values = dict(state.get("items", {}))

	def is_serving(self):
		return True

	async def prepare(self, state, mutation, serving_members):
		self.serving_members = serving_members
		return mutation

	async def apply(self, state, mutation):
		items = state.setdefault("items", {})
		if mutation.action == "delete":
			items.pop(mutation.key, None)
		else:
			items[mutation.key] = mutation.value
		self.values = dict(items)
		return {"item": mutation.key, "value": mutation.value}


def _configuration(tmp_path, member_count=1):
	peers = tuple(
		ClusterPeer(node_id=f"node-{index:03}", address=f"https://node-{index:03}.example.com")
		for index in range(1, member_count + 1)
	)
	return ClusterConfig(
		node_id="node-001",
		password="current-secret",
		previous_password="previous-secret",
		previous_password_valid_until=int(time.time()) + 600,
		state_path=tmp_path / "cluster-state.json",
		peers=peers,
	)


def test_snapshot_store_replaces_complete_state(tmp_path):
	store = SnapshotStore(tmp_path / "state.json")
	snapshot = ClusterSnapshot(generation=7, state={"items": {"erp": "2001:db8::1"}})

	store.save(snapshot)

	assert store.load() == snapshot
	assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
	("members", "required"),
	[(1, 1), (2, 2), (3, 3), (4, 3), (5, 3)],
)
def test_acknowledgement_policy(tmp_path, members, required):
	manager = ClusterManager(_configuration(tmp_path, members), _MemoryState())

	assert manager._required_acknowledgements == required

	asyncio.run(manager.client.aclose())


def test_one_node_mutation_increases_the_generation(tmp_path):
	async def run():
		state_machine = _MemoryState()
		manager = ClusterManager(_configuration(tmp_path), state_machine)
		await manager.start()

		result = await manager.mutate(Mutation(kind="items", action="update", key="erp", value="2001:db8::1"))

		assert result.generation == 1
		assert state_machine.values == {"erp": "2001:db8::1"}
		assert SnapshotStore(tmp_path / "cluster-state.json").load().generation == 1
		await manager.close()

	asyncio.run(run())


def test_four_nodes_accept_one_missing_acknowledgement(tmp_path):
	async def run():
		manager = ClusterManager(_configuration(tmp_path, 4), _MemoryState())
		manager.role = "leader"
		manager.leader_id = "node-001"
		manager.is_initialized = True
		manager.is_synchronized = True
		manager._replicate = AsyncMock(side_effect=[True, True, RuntimeError("offline")])

		result = await manager.mutate(Mutation(kind="items", action="update", key="erp", value="2001:db8::1"))

		assert result.generation == 1
		await manager.close()

	asyncio.run(run())


def test_five_nodes_accept_two_missing_acknowledgements(tmp_path):
	async def run():
		manager = ClusterManager(_configuration(tmp_path, 5), _MemoryState())
		manager.role = "leader"
		manager.leader_id = "node-001"
		manager.is_initialized = True
		manager.is_synchronized = True
		manager._replicate = AsyncMock(
			side_effect=[True, True, RuntimeError("offline"), RuntimeError("offline")]
		)

		result = await manager.mutate(Mutation(kind="items", action="update", key="erp", value="2001:db8::1"))

		assert result.generation == 1
		await manager.close()

	asyncio.run(run())


def test_three_nodes_reject_one_missing_acknowledgement(tmp_path):
	async def run():
		manager = ClusterManager(_configuration(tmp_path, 3), _MemoryState())
		manager.role = "leader"
		manager.leader_id = "node-001"
		manager.is_initialized = True
		manager.is_synchronized = True
		manager._replicate = AsyncMock(side_effect=[True, RuntimeError("offline")])

		with pytest.raises(HTTPException) as raised:
			await manager.mutate(Mutation(kind="items", action="update", key="erp", value="2001:db8::1"))

		assert raised.value.status_code == 503
		assert raised.value.detail["acknowledged"] == 2
		await manager.close()

	asyncio.run(run())


def test_vote_requires_a_current_generation(tmp_path):
	async def run():
		manager = ClusterManager(_configuration(tmp_path, 3), _MemoryState())
		manager.snapshot = ClusterSnapshot(term=2, generation=8)

		result = await manager.request_vote(VoteRequest(term=3, candidate_id="node-002", generation=7))

		assert result == {"term": 3, "granted": False}
		await manager.close()

	asyncio.run(run())


def test_vote_requires_a_current_mutation_term(tmp_path):
	async def run():
		manager = ClusterManager(_configuration(tmp_path, 3), _MemoryState())
		manager.snapshot = ClusterSnapshot(term=5, generation=7, mutation_term=4)

		result = await manager.request_vote(
			VoteRequest(
				term=6,
				candidate_id="node-002",
				generation=8,
				mutation_term=3,
			)
		)

		assert result == {"term": 6, "granted": False}
		await manager.close()

	asyncio.run(run())


def test_vote_rejects_a_node_outside_the_configured_membership(tmp_path):
	async def run():
		manager = ClusterManager(_configuration(tmp_path, 3), _MemoryState())
		manager.snapshot = ClusterSnapshot(term=2)

		result = await manager.request_vote(VoteRequest(term=9, candidate_id="node-004", generation=9))

		assert result == {"term": 2, "granted": False}
		assert manager.snapshot.term == 2
		await manager.close()

	asyncio.run(run())


def test_candidate_steps_down_for_a_higher_term(tmp_path):
	async def run():
		manager = ClusterManager(_configuration(tmp_path, 3), _MemoryState())
		manager.snapshot = ClusterSnapshot(term=2)
		manager._peer_post = AsyncMock(
			side_effect=[
				{"term": 5, "granted": False},
				{"term": 3, "granted": True},
			]
		)

		await manager._elect()

		assert manager.snapshot.term == 5
		assert manager.role == "follower"
		assert manager.leader_id == ""
		await manager.close()

	asyncio.run(run())


def test_bootstrap_does_not_invent_a_leader(tmp_path):
	async def run():
		manager = ClusterManager(_configuration(tmp_path, 3), _MemoryState())
		manager._peer_get = AsyncMock(
			side_effect=[
				{"node_id": "node-002", "leader_id": "", "generation": 0},
				{"node_id": "node-003", "leader_id": "", "generation": 0},
			]
		)

		await manager.bootstrap()

		assert manager.leader_id == ""
		await manager.close()

	asyncio.run(run())


def test_an_installed_snapshot_does_not_replace_the_current_vote(tmp_path):
	async def run():
		manager = ClusterManager(_configuration(tmp_path, 3), _MemoryState())
		manager.snapshot = ClusterSnapshot(term=8, voted_for="node-002", generation=3)

		await manager.install_snapshot(
			ClusterSnapshot(term=7, voted_for="node-003", generation=4, state={"items": {"erp": "::1"}})
		)

		assert manager.snapshot.term == 8
		assert manager.snapshot.voted_for == "node-002"
		assert manager.snapshot.generation == 4
		await manager.close()

	asyncio.run(run())


def test_heartbeat_repairs_a_different_operation_at_the_same_generation(tmp_path):
	async def run():
		manager = ClusterManager(_configuration(tmp_path, 3), _MemoryState())
		manager.snapshot = ClusterSnapshot(term=4, generation=7, operation_id="local-operation")
		manager.is_synchronized = True
		manager._synchronize_from_leader = AsyncMock()

		await manager.heartbeat(
			HeartbeatRequest(
				term=4,
				leader_id="node-002",
				generation=7,
				operation_id="leader-operation",
			)
		)
		await asyncio.sleep(0)

		assert not manager.is_synchronized
		manager._synchronize_from_leader.assert_awaited_once()
		await manager.close()

	asyncio.run(run())


def test_elected_leader_snapshot_can_replace_an_uncommitted_generation(tmp_path):
	async def run():
		manager = ClusterManager(_configuration(tmp_path, 3), _MemoryState())
		manager.snapshot = ClusterSnapshot(generation=8, operation_id="uncommitted")

		await manager.install_snapshot(
			ClusterSnapshot(generation=7, operation_id="committed", state={"items": {"erp": "::1"}}),
			allow_older=True,
		)

		assert manager.snapshot.generation == 7
		assert manager.snapshot.operation_id == "committed"
		await manager.close()

	asyncio.run(run())


def test_only_generation_conflicts_trigger_snapshot_repair(tmp_path):
	manager = ClusterManager(_configuration(tmp_path, 3), _MemoryState())
	request = httpx.Request("POST", "https://node-002.example.com/internal/cluster/replicate")
	generation_response = httpx.Response(
		409,
		request=request,
		json={"detail": {"error": "generation mismatch", "generation": 4}},
	)
	leader_response = httpx.Response(
		409,
		request=request,
		json={"detail": {"leader_id": "node-003"}},
	)

	assert manager._is_generation_conflict(generation_response)
	assert not manager._is_generation_conflict(leader_response)

	asyncio.run(manager.close())


def test_internal_auth_accepts_current_and_previous_passwords(tmp_path):
	manager = ClusterManager(_configuration(tmp_path, 3), _MemoryState())

	manager.authenticate("current-secret")
	manager.authenticate("previous-secret")
	with pytest.raises(HTTPException):
		manager.authenticate("expired-secret")

	asyncio.run(manager.client.aclose())


def test_internal_auth_rejects_the_previous_password_after_expiry(tmp_path):
	configuration = _configuration(tmp_path, 3)
	configuration = replace(configuration, previous_password_valid_until=int(time.time()) - 1)
	manager = ClusterManager(configuration, _MemoryState())

	with pytest.raises(HTTPException):
		manager.authenticate("previous-secret")

	asyncio.run(manager.client.aclose())


def test_peer_reads_retry_with_the_previous_password(tmp_path):
	async def run():
		headers = []

		def respond(request):
			headers.append(request.headers["X-Atlas-Cluster-Password"])
			if len(headers) == 1:
				return httpx.Response(401)
			return httpx.Response(200, json={"generation": 4})

		manager = ClusterManager(_configuration(tmp_path, 2), _MemoryState())
		await manager.client.aclose()
		manager.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

		result = await manager._peer_get(manager.configuration.peers[1], "/internal/cluster/status")

		assert result == {"generation": 4}
		assert headers == ["current-secret", "previous-secret"]
		await manager.close()

	asyncio.run(run())


def test_peer_reads_do_not_retry_with_an_expired_password(tmp_path):
	async def run():
		headers = []

		def respond(request):
			headers.append(request.headers["X-Atlas-Cluster-Password"])
			return httpx.Response(401)

		configuration = replace(
			_configuration(tmp_path, 2), previous_password_valid_until=int(time.time()) - 1
		)
		manager = ClusterManager(configuration, _MemoryState())
		await manager.client.aclose()
		manager.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

		with pytest.raises(httpx.HTTPStatusError):
			await manager._peer_get(manager.configuration.peers[1], "/internal/cluster/status")

		assert headers == ["current-secret"]
		await manager.close()

	asyncio.run(run())


def _follower(tmp_path):
	manager = ClusterManager(_configuration(tmp_path, 3), _MemoryState())
	manager.leader_id = "node-002"
	manager.is_initialized = True
	manager.is_synchronized = True
	return manager


def _leader_response(status_code, payload):
	request = httpx.Request("POST", "https://node-002.example.com/internal/cluster/mutate")
	return httpx.Response(status_code, json=payload, request=request)


def test_a_forwarded_write_keeps_the_leader_conflict_status(tmp_path):
	"""A rejected mutation must not look like an unreachable leader."""

	async def run():
		manager = _follower(tmp_path)
		response = _leader_response(409, {"detail": {"error": "generation mismatch", "generation": 7}})
		manager._peer_post = AsyncMock(
			side_effect=httpx.HTTPStatusError("conflict", request=response.request, response=response)
		)

		with pytest.raises(HTTPException) as raised:
			await manager.mutate(Mutation(kind="items", action="delete", key="erp"))

		assert raised.value.status_code == 409
		assert raised.value.detail == {"error": "generation mismatch", "generation": 7}
		await manager.close()

	asyncio.run(run())


def test_a_forwarded_write_reports_an_unreachable_leader(tmp_path):
	async def run():
		manager = _follower(tmp_path)
		manager._peer_post = AsyncMock(side_effect=httpx.ReadTimeout("slow"))

		with pytest.raises(HTTPException) as raised:
			await manager.mutate(Mutation(kind="items", action="delete", key="erp"))

		assert raised.value.status_code == 503
		assert raised.value.detail == {"error": "leader unavailable"}
		await manager.close()

	asyncio.run(run())


def test_a_cluster_password_failure_is_not_the_caller_fault(tmp_path):
	"""The caller authenticated. Only the peer request failed."""

	async def run():
		manager = _follower(tmp_path)
		response = _leader_response(401, {"detail": "unauthorized"})
		manager._peer_post = AsyncMock(
			side_effect=httpx.HTTPStatusError("denied", request=response.request, response=response)
		)

		with pytest.raises(HTTPException) as raised:
			await manager.mutate(Mutation(kind="items", action="delete", key="erp"))

		assert raised.value.status_code == 502
		await manager.close()

	asyncio.run(run())


def test_the_forward_budget_covers_a_peer_repair(tmp_path):
	"""A repaired peer needs three sequential leader calls. Each one can retry after a password change."""
	assert FORWARDED_WRITE_TIMEOUT_SECONDS > PEER_REPAIR_CALLS * PEER_CALL_ATTEMPTS * PEER_TIMEOUT_SECONDS


def test_the_leader_prepares_mutations_with_the_members_that_serve(tmp_path):
	async def run():
		state_machine = _MemoryState()
		manager = ClusterManager(_configuration(tmp_path, 4), state_machine)
		manager.role = "leader"
		manager.leader_id = "node-001"

		async def peer_post(peer, path, body, timeout_seconds=PEER_TIMEOUT_SECONDS):
			if peer.node_id == "node-004":
				raise httpx.ConnectError("down")
			# node-003 answers, but its service is broken.
			return {"term": 0, "accepted": True, "serving": peer.node_id != "node-003"}

		manager._peer_post = peer_post
		await manager._send_heartbeats()
		await manager.mutate(Mutation(kind="items", action="update", key="erp", value="2001:db8::1"))
		return state_machine.serving_members

	assert asyncio.run(run()) == frozenset({"node-001", "node-002"})
