from typing import Annotated

from fastapi import APIRouter, Depends, Header

from .cluster import (
	ClusterManager,
	ClusterSnapshot,
	HeartbeatRequest,
	Mutation,
	ReplicationRequest,
	VoteRequest,
)


def create_cluster_router(cluster: ClusterManager) -> APIRouter:
	"""Return the peer-to-peer routes. Only callers with the cluster password reach them."""

	def require_cluster_password(
		password: Annotated[str | None, Header(alias="X-Atlas-Cluster-Password")] = None,
	) -> None:
		cluster.authenticate(password)

	router = APIRouter(
		prefix="/internal/cluster",
		dependencies=[Depends(require_cluster_password)],
		include_in_schema=False,
	)

	@router.get("/status")
	async def internal_cluster_status() -> dict[str, object]:
		return cluster.status()

	@router.get("/snapshot")
	async def internal_cluster_snapshot() -> ClusterSnapshot:
		return cluster.snapshot

	@router.post("/snapshot")
	async def install_cluster_snapshot(snapshot: ClusterSnapshot) -> dict[str, int]:
		await cluster.install_snapshot(snapshot)
		return {"generation": cluster.snapshot.generation}

	@router.post("/vote")
	async def request_cluster_vote(request: VoteRequest) -> dict[str, object]:
		return await cluster.request_vote(request)

	@router.post("/heartbeat")
	async def receive_cluster_heartbeat(request: HeartbeatRequest) -> dict[str, object]:
		return await cluster.heartbeat(request)

	@router.post("/replicate")
	async def replicate_cluster_mutation(request: ReplicationRequest) -> dict[str, int]:
		result = await cluster.apply_replication(request)
		return {"generation": result.generation}

	@router.post("/mutate")
	async def forward_cluster_mutation(mutation: Mutation) -> dict[str, object]:
		result = await cluster.mutate(mutation)
		return {"body": result.body, "generation": result.generation}

	return router
