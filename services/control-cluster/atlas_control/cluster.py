import asyncio
import hmac
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

import httpx
from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from .config import ClusterConfig, ClusterPeer

HEARTBEAT_INTERVAL_SECONDS = 0.1
# A member that reported serving in a heartbeat this recently counts as serving.
MEMBER_LIVE_SECONDS = 1.0
ELECTION_TIMEOUT_SECONDS = (0.3, 0.45)
PEER_TIMEOUT_SECONDS = 0.2
# One peer call retries once with the previous cluster password.
PEER_CALL_ATTEMPTS = 2
# The leader needs three sequential peer calls to repair one stale peer.
PEER_REPAIR_CALLS = 3
FORWARDED_WRITE_TIMEOUT_SECONDS = PEER_REPAIR_CALLS * PEER_CALL_ATTEMPTS * PEER_TIMEOUT_SECONDS + 0.3


class Mutation(BaseModel):
	"""One ordered state mutation. The state machine validates `kind` and `value`."""

	model_config = ConfigDict(extra="forbid")

	kind: str
	action: Literal["replace", "update", "delete"]
	key: str = ""
	value: Any = None
	operation_id: str = ""


class ReplicationRequest(BaseModel):
	"""One mutation with its assigned generation."""

	model_config = ConfigDict(extra="forbid")

	term: int
	leader_id: str
	base_generation: int
	generation: int
	mutation: Mutation


class VoteRequest(BaseModel):
	"""One request for an election vote."""

	model_config = ConfigDict(extra="forbid")

	term: int
	candidate_id: str
	generation: int
	mutation_term: int = 0


class HeartbeatRequest(BaseModel):
	"""One leader heartbeat."""

	model_config = ConfigDict(extra="forbid")

	term: int
	leader_id: str
	generation: int
	operation_id: str = ""


class ClusterSnapshot(BaseModel):
	"""Complete durable cluster state: election data and the service state."""

	model_config = ConfigDict(extra="forbid")

	term: int = 0
	voted_for: str = ""
	generation: int = 0
	mutation_term: int = 0
	operation_id: str = ""
	state: dict[str, Any] = Field(default_factory=dict)


class StateMachine(Protocol):
	"""The service state that the cluster replicates. Every mutation must be safe to repeat."""

	async def initial_state(self) -> dict[str, Any]:
		"""Return the state of a node that has no stored snapshot."""

	async def restore(self, state: dict[str, Any]) -> None:
		"""Apply one complete state to the service."""

	def is_serving(self) -> bool:
		"""Report whether this node serves its traffic. Heartbeat replies carry it to the leader."""

	async def prepare(
		self, state: dict[str, Any], mutation: Mutation, serving_members: frozenset[str]
	) -> Mutation:
		"""Resolve a choice that only the leader can make, such as placement on a serving member.

		The leader runs it once before replication, so every node applies the same result."""

	async def apply(self, state: dict[str, Any], mutation: Mutation) -> dict[str, object]:
		"""Validate one mutation, apply it to the service and to `state`, and return the response body."""


@dataclass(frozen=True)
class MutationResult:
	"""Result returned after a replicated mutation."""

	body: dict[str, object]
	generation: int


class SnapshotStore:
	"""Persist one complete cluster snapshot."""

	def __init__(self, path: Path):
		self.path = path

	def load(self) -> ClusterSnapshot | None:
		try:
			return ClusterSnapshot.model_validate_json(self.path.read_text())
		except FileNotFoundError:
			return None

	def save(self, snapshot: ClusterSnapshot) -> None:
		self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
		temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
		with temporary_path.open("w") as target:
			target.write(snapshot.model_dump_json())
			target.write("\n")
			target.flush()
			os.fsync(target.fileno())
		os.chmod(temporary_path, 0o600)
		os.replace(temporary_path, self.path)


class ClusterManager:
	"""Elect one leader and replicate state machine mutations."""

	def __init__(self, configuration: ClusterConfig, state_machine: StateMachine):
		self.configuration = configuration
		self.state_machine = state_machine
		self.store = SnapshotStore(configuration.state_path)
		self.snapshot = ClusterSnapshot()
		self.role = "follower"
		self.leader_id = ""
		self.is_initialized = False
		self.is_synchronized = False
		self.last_heartbeat = 0.0
		self.election_deadline = 0.0
		self.mutation_lock = asyncio.Lock()
		self.persistence_lock = asyncio.Lock()
		self.synchronization_lock = asyncio.Lock()
		self.client = httpx.AsyncClient(timeout=PEER_TIMEOUT_SECONDS)
		self.background_task: asyncio.Task[None] | None = None
		self.acknowledged_at: dict[str, float] = {}

	@property
	def is_enabled(self) -> bool:
		"""Report whether cluster coordination is configured."""
		return self.configuration.is_enabled

	@property
	def is_ready(self) -> bool:
		"""Report whether this node can serve synchronized state."""
		if not self.is_initialized:
			return False
		return self.is_synchronized and (not self.is_enabled or bool(self.leader_id))

	@property
	def serving_members(self) -> frozenset[str]:
		"""Return the members that serve traffic: this node by its own check, peers by a recent heartbeat.

		Only the leader tracks peers."""
		now = time.monotonic()
		serving = {node_id for node_id, at in self.acknowledged_at.items() if now - at <= MEMBER_LIVE_SECONDS}
		if self.state_machine.is_serving():
			serving.add(self.configuration.node_id)
		return frozenset(serving)

	@property
	def member_count(self) -> int:
		"""Return the configured voting-member count."""
		return len(self.configuration.peers) if self.is_enabled else 1

	async def start(self) -> None:
		"""Restore local state and start cluster coordination."""
		stored = await asyncio.to_thread(self.store.load)
		if stored is None:
			stored = ClusterSnapshot(state=await self.state_machine.initial_state())
			await self._save(stored)
		self.snapshot = stored
		await self.state_machine.restore(stored.state)
		self.is_initialized = True
		self.is_synchronized = True
		self._reset_election_deadline()
		if not self.is_enabled or self.member_count == 1:
			self.role = "leader"
			self.leader_id = self.configuration.node_id
			return
		await self.bootstrap()
		self.background_task = asyncio.create_task(self._run())

	async def close(self) -> None:
		"""Stop coordination and close peer connections."""
		if self.background_task:
			self.background_task.cancel()
			try:
				await self.background_task
			except asyncio.CancelledError:
				pass
		await self.client.aclose()

	async def mutate(self, mutation: Mutation) -> MutationResult:
		"""Forward or replicate one public mutation."""
		mutation.operation_id = mutation.operation_id or str(uuid4())
		if self.is_enabled and self.leader_id != self.configuration.node_id:
			if not self.leader_id:
				raise HTTPException(status_code=503, detail={"error": "leader unavailable"})
			return await self._forward(mutation)
		async with self.mutation_lock:
			return await self._replicate_mutation(mutation)

	async def apply_replication(self, request: ReplicationRequest) -> MutationResult:
		"""Apply one leader-assigned mutation."""
		async with self.mutation_lock:
			if not self._is_member(request.leader_id):
				raise HTTPException(status_code=409, detail={"error": "leader is not a member"})
			if request.term < self.snapshot.term:
				raise HTTPException(status_code=409, detail={"term": self.snapshot.term})
			if request.term > self.snapshot.term:
				await self._set_term(request.term)
			if self.leader_id and self.leader_id != request.leader_id:
				raise HTTPException(status_code=409, detail={"leader_id": self.leader_id})
			self.leader_id = request.leader_id
			if request.generation == self.snapshot.generation:
				if request.mutation.operation_id == self.snapshot.operation_id:
					return MutationResult({}, request.generation)
				raise HTTPException(
					status_code=409,
					detail={"error": "generation mismatch", "generation": self.snapshot.generation},
				)
			if request.base_generation != self.snapshot.generation:
				raise HTTPException(
					status_code=409,
					detail={"error": "generation mismatch", "generation": self.snapshot.generation},
				)
			body = await self._apply(request.mutation, request.generation)
			return MutationResult(body, request.generation)

	async def install_snapshot(self, snapshot: ClusterSnapshot, allow_older: bool = False) -> None:
		"""Install one complete snapshot."""
		async with self.mutation_lock:
			if snapshot.generation < self.snapshot.generation and not allow_older:
				raise HTTPException(
					status_code=409,
					detail={"error": "generation is older", "generation": self.snapshot.generation},
				)
			installed = snapshot.model_copy(
				update={
					"term": max(snapshot.term, self.snapshot.term),
					"voted_for": self.snapshot.voted_for if self.snapshot.term >= snapshot.term else "",
				}
			)
			await self._save(installed)
			await self.state_machine.restore(installed.state)
			self.snapshot = installed
			self.is_synchronized = True

	async def request_vote(self, request: VoteRequest) -> dict[str, object]:
		"""Grant at most one vote in a term to an up-to-date candidate."""
		if not self._is_member(request.candidate_id):
			return {"term": self.snapshot.term, "granted": False}
		if request.term < self.snapshot.term:
			return {"term": self.snapshot.term, "granted": False}
		if request.term > self.snapshot.term:
			await self._set_term(request.term)
		can_vote = not self.snapshot.voted_for or self.snapshot.voted_for == request.candidate_id
		is_current = (request.mutation_term, request.generation) >= (
			self.snapshot.mutation_term,
			self.snapshot.generation,
		)
		granted = can_vote and is_current
		if granted:
			self.snapshot.voted_for = request.candidate_id
			await self._save(self.snapshot)
			self._reset_election_deadline()
		return {"term": self.snapshot.term, "granted": granted}

	async def heartbeat(self, request: HeartbeatRequest) -> dict[str, object]:
		"""Accept a current leader heartbeat."""
		if not self._is_member(request.leader_id):
			return {"term": self.snapshot.term, "accepted": False, "generation": self.snapshot.generation}
		if request.term < self.snapshot.term:
			return {"term": self.snapshot.term, "accepted": False, "generation": self.snapshot.generation}
		if request.term > self.snapshot.term:
			await self._set_term(request.term)
		self.role = "follower"
		self.leader_id = request.leader_id
		self.last_heartbeat = asyncio.get_running_loop().time()
		self._reset_election_deadline()
		if (
			request.generation != self.snapshot.generation
			or request.operation_id != self.snapshot.operation_id
		):
			self.is_synchronized = False
			asyncio.create_task(self._synchronize_from_leader())
		return {
			"term": self.snapshot.term,
			"accepted": True,
			"generation": self.snapshot.generation,
			"operation_id": self.snapshot.operation_id,
			"serving": self.state_machine.is_serving(),
		}

	async def bootstrap(self) -> None:
		"""Restore the newest snapshot exposed by configured peers."""
		peers = self._other_peers()
		statuses = await asyncio.gather(
			*(self._peer_get(peer, "/internal/cluster/status") for peer in peers),
			return_exceptions=True,
		)
		available = [
			(peer, item) for peer, item in zip(peers, statuses, strict=True) if isinstance(item, dict)
		]
		if not available:
			return
		source_peer, newest = max(available, key=lambda item: int(item[1].get("generation", -1)))
		leader_id = str(newest.get("leader_id") or "")
		if not self._is_member(leader_id):
			leader_id = ""
		if int(newest.get("generation", 0)) > self.snapshot.generation:
			response = await self._peer_get(source_peer, "/internal/cluster/snapshot")
			await self.install_snapshot(ClusterSnapshot.model_validate(response))
		self.leader_id = leader_id
		self.is_synchronized = int(newest.get("generation", 0)) == self.snapshot.generation
		self._reset_election_deadline()

	def status(self) -> dict[str, object]:
		"""Return public cluster status without secrets."""
		return {
			"node_id": self.configuration.node_id,
			"role": self.role,
			"leader_id": self.leader_id,
			"term": self.snapshot.term,
			"generation": self.snapshot.generation,
			"members": self.member_count,
			"ready": self.is_ready,
		}

	def authenticate(self, password: str | None) -> None:
		"""Authenticate one internal cluster request."""
		accepted = [self.configuration.password]
		if self._has_valid_previous_password:
			accepted.append(self.configuration.previous_password)
		if not password or not any(secret and hmac.compare_digest(password, secret) for secret in accepted):
			raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

	async def _run(self) -> None:
		while True:
			if self.role == "leader":
				await self._send_heartbeats()
				await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
				continue
			if asyncio.get_running_loop().time() >= self.election_deadline:
				await self._elect()
			await asyncio.sleep(0.05)

	async def _synchronize_from_leader(self) -> None:
		async with self.synchronization_lock:
			peer = self._peer(self.leader_id)
			if peer is None:
				return
			try:
				response = await self._peer_get(peer, "/internal/cluster/snapshot")
				await self.install_snapshot(ClusterSnapshot.model_validate(response), allow_older=True)
			except httpx.HTTPError, HTTPException, ValueError:
				return

	async def _elect(self) -> None:
		self.role = "candidate"
		self.leader_id = ""
		await self._set_term(self.snapshot.term + 1)
		self.snapshot.voted_for = self.configuration.node_id
		await self._save(self.snapshot)
		request = VoteRequest(
			term=self.snapshot.term,
			candidate_id=self.configuration.node_id,
			generation=self.snapshot.generation,
			mutation_term=self.snapshot.mutation_term,
		)
		responses = await asyncio.gather(
			*(
				self._peer_post(peer, "/internal/cluster/vote", request.model_dump())
				for peer in self._other_peers()
			),
			return_exceptions=True,
		)
		higher_term = max(
			(int(item.get("term", 0)) for item in responses if isinstance(item, dict)),
			default=0,
		)
		if higher_term > self.snapshot.term:
			await self._set_term(higher_term)
			self._reset_election_deadline()
			return
		votes = 1 + sum(bool(item.get("granted")) for item in responses if isinstance(item, dict))
		if votes >= self._majority:
			self.role = "leader"
			self.leader_id = self.configuration.node_id
			return
		self.role = "follower"
		self._reset_election_deadline()

	async def _send_heartbeats(self) -> None:
		request = HeartbeatRequest(
			term=self.snapshot.term,
			leader_id=self.configuration.node_id,
			generation=self.snapshot.generation,
			operation_id=self.snapshot.operation_id,
		)
		peers = self._other_peers()
		responses = await asyncio.gather(
			*(self._peer_post(peer, "/internal/cluster/heartbeat", request.model_dump()) for peer in peers),
			return_exceptions=True,
		)
		higher_term = max(
			(int(item.get("term", 0)) for item in responses if isinstance(item, dict)),
			default=0,
		)
		if higher_term > self.snapshot.term:
			await self._set_term(higher_term)
			self._reset_election_deadline()
			return
		now = time.monotonic()
		for peer, item in zip(peers, responses, strict=True):
			if isinstance(item, dict) and item.get("accepted") and item.get("serving"):
				self.acknowledged_at[peer.node_id] = now
			else:
				self.acknowledged_at.pop(peer.node_id, None)
		accepted = 1 + sum(bool(item.get("accepted")) for item in responses if isinstance(item, dict))
		if accepted < self._majority:
			self.role = "follower"
			self.leader_id = ""
			self._reset_election_deadline()

	async def _replicate_mutation(self, mutation: Mutation) -> MutationResult:
		mutation = await self.state_machine.prepare(self.snapshot.state, mutation, self.serving_members)
		base_snapshot = self.snapshot.model_copy(deep=True)
		generation = base_snapshot.generation + 1
		body = await self._apply(mutation, generation)
		request = ReplicationRequest(
			term=self.snapshot.term,
			leader_id=self.configuration.node_id,
			base_generation=base_snapshot.generation,
			generation=generation,
			mutation=mutation,
		)
		responses = await asyncio.gather(
			*(self._replicate(peer, request, base_snapshot) for peer in self._other_peers()),
			return_exceptions=True,
		)
		acknowledged = 1 + sum(item is True for item in responses)
		if acknowledged < self._required_acknowledgements:
			raise HTTPException(
				status_code=503,
				detail={
					"error": "replication threshold not met",
					"generation": generation,
					"required": self._required_acknowledgements,
					"acknowledged": acknowledged,
					"retryable": True,
				},
			)
		return MutationResult(body, generation)

	async def _replicate(
		self, peer: ClusterPeer, request: ReplicationRequest, base_snapshot: ClusterSnapshot
	) -> bool:
		try:
			await self._peer_post(peer, "/internal/cluster/replicate", request.model_dump())
			return True
		except httpx.HTTPStatusError as error:
			if not self._is_generation_conflict(error.response):
				raise
		await self._peer_post(peer, "/internal/cluster/snapshot", base_snapshot.model_dump())
		await self._peer_post(peer, "/internal/cluster/replicate", request.model_dump())
		return True

	async def _forward(self, mutation: Mutation) -> MutationResult:
		peer = self._peer(self.leader_id)
		if peer is None:
			raise HTTPException(status_code=503, detail={"error": "leader unavailable"})
		try:
			body = await self._peer_post(
				peer,
				"/internal/cluster/mutate",
				mutation.model_dump(),
				timeout_seconds=FORWARDED_WRITE_TIMEOUT_SECONDS,
			)
		except httpx.HTTPStatusError as error:
			raise self._forwarded_error(error.response) from error
		except httpx.HTTPError as error:
			raise HTTPException(status_code=503, detail={"error": "leader unavailable"}) from error
		return MutationResult(dict(body.get("body", {})), int(body["generation"]))

	@staticmethod
	def _forwarded_error(response: httpx.Response) -> HTTPException:
		"""Return the leader answer as an error the caller can act on."""
		if response.status_code in {401, 403}:
			return HTTPException(status_code=502, detail={"error": "cluster authentication failed"})
		try:
			detail = response.json()["detail"]
		except ValueError, KeyError:
			detail = {"error": "leader rejected the mutation"}
		return HTTPException(status_code=response.status_code, detail=detail)

	async def _apply(self, mutation: Mutation, generation: int) -> dict[str, object]:
		body = await self.state_machine.apply(self.snapshot.state, mutation)
		self.snapshot.generation = generation
		self.snapshot.mutation_term = self.snapshot.term
		self.snapshot.operation_id = mutation.operation_id
		await self._save(self.snapshot)
		return body

	async def _set_term(self, term: int) -> None:
		self.snapshot.term = term
		self.snapshot.voted_for = ""
		self.role = "follower"
		self.leader_id = ""
		await self._save(self.snapshot)

	async def _save(self, snapshot: ClusterSnapshot) -> None:
		async with self.persistence_lock:
			await asyncio.to_thread(self.store.save, snapshot.model_copy(deep=True))

	async def _peer_get(self, peer: ClusterPeer, path: str) -> dict[str, object]:
		response = await self.client.get(f"{peer.address}{path}", headers=self._headers)
		if response.status_code == 401 and self._has_valid_previous_password:
			response = await self.client.get(
				f"{peer.address}{path}",
				headers={"X-Atlas-Cluster-Password": self.configuration.previous_password},
			)
		response.raise_for_status()
		return response.json()

	async def _peer_post(
		self,
		peer: ClusterPeer,
		path: str,
		body: dict[str, object],
		timeout_seconds: float = PEER_TIMEOUT_SECONDS,
	) -> dict[str, object]:
		response = await self.client.post(
			f"{peer.address}{path}",
			headers=self._headers,
			json=body,
			timeout=timeout_seconds,
		)
		if response.status_code == 401 and self._has_valid_previous_password:
			response = await self.client.post(
				f"{peer.address}{path}",
				headers={"X-Atlas-Cluster-Password": self.configuration.previous_password},
				json=body,
				timeout=timeout_seconds,
			)
		response.raise_for_status()
		return response.json() if response.content else {}

	@property
	def _headers(self) -> dict[str, str]:
		return {"X-Atlas-Cluster-Password": self.configuration.password}

	@property
	def _has_valid_previous_password(self) -> bool:
		return bool(
			self.configuration.previous_password
			and time.time() <= self.configuration.previous_password_valid_until
		)

	@property
	def _majority(self) -> int:
		return self.member_count // 2 + 1

	@property
	def _required_acknowledgements(self) -> int:
		return self.member_count if self.member_count <= 3 else self._majority

	def _other_peers(self) -> tuple[ClusterPeer, ...]:
		return tuple(peer for peer in self.configuration.peers if peer.node_id != self.configuration.node_id)

	def _peer(self, node_id: str) -> ClusterPeer | None:
		return next((peer for peer in self.configuration.peers if peer.node_id == node_id), None)

	def _is_member(self, node_id: str) -> bool:
		return not self.is_enabled or self._peer(node_id) is not None

	@staticmethod
	def _is_generation_conflict(response: httpx.Response) -> bool:
		if response.status_code != 409:
			return False
		try:
			return response.json().get("detail", {}).get("error") == "generation mismatch"
		except ValueError:
			return False

	def _reset_election_deadline(self) -> None:
		delay = random.uniform(*ELECTION_TIMEOUT_SECONDS)
		self.election_deadline = asyncio.get_running_loop().time() + delay
