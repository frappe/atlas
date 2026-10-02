from contextlib import asynccontextmanager
from typing import Annotated

from atlas_control.auth import Authentication, Authorization
from atlas_control.cluster import ClusterManager, Mutation
from atlas_control.routes import create_cluster_router
from fastapi import Depends, FastAPI, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .config import ConfigError, load, load_auth
from .peers import PEERS_KIND, PeerState, get_identity, validate_device

SCOPES = frozenset({"*", "peers:*", "peers:read", "peers:update"})

try:
	config = load()
except ConfigError as error:
	raise SystemExit(f"atlas-wg-gateway: {error}") from error

auth = Authentication(load_auth, SCOPES, "atlas-wg-gateway")
peer_state = PeerState(config)
cluster = ClusterManager(config.cluster, peer_state)


class PeerRegistration(BaseModel):
	"""One customer device."""

	model_config = ConfigDict(extra="forbid")

	tenant_id: int = Field(description="Tenant that owns the VMs the device reaches.")
	client_id: int = Field(description="Device number, unique within the tenant.")
	public_key: str = Field(description="WireGuard public key of the device.")


class Device(PeerRegistration):
	"""One device of a restored table."""

	node_id: str = Field(
		default="", description="Node that served the device. Keep it, so the address stays."
	)


class PeerTable(BaseModel):
	"""The complete device table, for a restore."""

	model_config = ConfigDict(extra="forbid")

	peers: list[Device] = Field(
		description="Every device. A device without node_id is assigned like a new one."
	)


@asynccontextmanager
async def lifespan(_: FastAPI):
	await cluster.start()
	try:
		yield
	finally:
		await cluster.close()


app = FastAPI(
	title="Atlas WireGuard gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
)
app.include_router(create_cluster_router(cluster))
GatewayAuthorization = Annotated[Authorization, Depends(auth.require_request)]


@app.get("/healthz")
async def healthz() -> Response:
	"""Report whether this node serves its devices: state loaded and wg0 configured."""
	is_healthy = cluster.is_initialized and peer_state.is_interface_ready()
	code = status.HTTP_204_NO_CONTENT if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE
	return Response(status_code=code)


@app.get("/readyz")
async def readyz() -> Response:
	"""Report whether this node can accept writes. The regional DNS health check uses it."""
	code = status.HTTP_204_NO_CONTENT if cluster.is_ready else status.HTTP_503_SERVICE_UNAVAILABLE
	return Response(status_code=code)


@app.post("/v1/peers")
async def register_peer(
	registration: PeerRegistration, authorization: GatewayAuthorization
) -> dict[str, object]:
	"""Register one device on the live node with the fewest devices, and return its WireGuard settings."""
	authorization.require("peers", "update")
	validate_device(registration.tenant_id, registration.client_id, registration.public_key)
	identity = get_identity(registration.tenant_id, registration.client_id)

	# Every request goes through the cluster: a retry must also retry a write that missed replication.
	value = {"public_key": registration.public_key}
	result = await cluster.mutate(Mutation(kind=PEERS_KIND, action="update", key=identity, value=value))
	return result.body


@app.delete("/v1/peers/{tenant_id}/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_peer(tenant_id: int, client_id: int, authorization: GatewayAuthorization) -> Response:
	"""Remove one device. A missing device is not an error."""
	authorization.require("peers", "update")
	await cluster.mutate(Mutation(kind=PEERS_KIND, action="delete", key=get_identity(tenant_id, client_id)))
	return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.put("/v1/peers")
async def replace_peers(table: PeerTable, authorization: GatewayAuthorization) -> dict[str, object]:
	"""Replace the complete device table, such as after a restore."""
	authorization.require("peers", "update")
	value = PeerState.build_table([device.model_dump() for device in table.peers])
	result = await cluster.mutate(Mutation(kind=PEERS_KIND, action="replace", value=value))
	return result.body


@app.get("/v1/peers")
async def list_peers(authorization: GatewayAuthorization) -> list[dict[str, object]]:
	"""Return the settings of every registered device."""
	authorization.require("peers", "read")
	peers = cluster.snapshot.state.get(PEERS_KIND, {})
	return [
		peer_state.get_credentials(identity, peer) | {"node_id": peer["node_id"]}
		for identity, peer in sorted(peers.items())
	]
