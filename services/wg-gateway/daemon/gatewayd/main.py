from contextlib import asynccontextmanager
from typing import Annotated

from atlas_control import docs
from atlas_control.auth import Authentication, Authorization
from atlas_control.cluster import ClusterManager, Mutation
from atlas_control.routes import create_cluster_router
from fastapi import Depends, FastAPI, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .config import ConfigError, load, load_auth
from .peers import PEERS_KIND, PeerState, get_identity, validate_device

SCOPES = frozenset({"*", "peers:*", "peers:read", "peers:update"})
PEER_KEY_EXAMPLE = "HIgo9xNzJMWLKASShiTqIybxZ0U3wGLiUeJ1PKf8ykw="
NODE_KEY_EXAMPLE = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
ERROR_RESPONSES = {
	401: {"description": "The bearer credential is missing or not valid."},
	403: {"description": "The token lacks the required scope."},
	503: {"description": "No leader or no serving node accepts the write. Retry the request."},
}

try:
	config = load()
except ConfigError as error:
	raise SystemExit(f"atlas-wg-gateway: {error}") from error

auth = Authentication(load_auth, SCOPES, "atlas-wg-gateway")
peer_state = PeerState(config)
cluster = ClusterManager(config.cluster, peer_state)


class PeerIdentity(BaseModel):
	"""One peer, by tenant and client."""

	model_config = ConfigDict(extra="forbid")

	tenant_id: int = Field(examples=[42], description="Tenant that owns the VMs the peer reaches.")
	client_id: int = Field(examples=[9], description="Peer number, unique within the tenant.")


class PeerRegistration(PeerIdentity):
	"""One peer to register."""

	public_key: str = Field(examples=[PEER_KEY_EXAMPLE], description="WireGuard public key of the peer.")


class RestoredPeer(PeerRegistration):
	"""One peer of a restored table."""

	node_id: str = Field(
		default="",
		examples=["wireguard-002"],
		description="Node that served the peer. Keep it, so the address stays.",
	)


class PeerTable(BaseModel):
	"""The complete peer table, for a restore."""

	model_config = ConfigDict(extra="forbid")

	peers: list[RestoredPeer] = Field(
		description="Every peer. A peer without node_id is assigned like a new one."
	)


class PeerSettings(BaseModel):
	"""The WireGuard settings of one peer."""

	tenant_id: int = Field(examples=[42], description="Tenant of the peer.")
	client_id: int = Field(examples=[9], description="Peer number within the tenant.")
	address: str = Field(examples=["fdac:1:2:0:2a::9/128"], description="Interface address of the peer.")
	allowed_ips: list[str] = Field(
		examples=[["fdaa:1:0:2a::/64"]],
		description="The VM addresses of the tenant. The peer puts them in its own AllowedIPs.",
	)
	endpoint: str = Field(
		examples=["wireguard-002.par-1.example.com:51820"],
		description="Node that serves the peer. Empty when that node was archived.",
	)
	public_key: str = Field(
		examples=[NODE_KEY_EXAMPLE], description="Public key of that node. Empty when that node was archived."
	)


class ListedPeer(PeerSettings):
	"""One registered peer and the node that serves it."""

	node_id: str = Field(examples=["wireguard-002"], description="Node that serves the peer.")


class TableReplaced(BaseModel):
	"""Result of a table restore."""

	peers: int = Field(examples=[1], description="Number of peers in the restored table.")


@asynccontextmanager
async def lifespan(_: FastAPI):
	await cluster.start()
	try:
		yield
	finally:
		await cluster.close()


app = FastAPI(
	title="Atlas WireGuard gateway",
	description="Register customer WireGuard peers with the regional gateway cluster. Any ready node accepts a write.",
	lifespan=lifespan,
	generate_unique_id_function=docs.operation_id,
	docs_url=None,
	redoc_url=None,
	openapi_url=None,
	openapi_tags=[
		{"name": "Health", "description": "Use these routes for liveness and readiness checks."},
		{"name": "Peers", "description": "Register, list, restore, and remove customer WireGuard peers."},
	],
)
app.include_router(create_cluster_router(cluster))
GatewayAuthorization = Annotated[Authorization, Depends(auth.require_request)]


@app.get("/healthz", tags=["Health"], summary="Check node health")
async def healthz() -> Response:
	"""Report whether this node serves its peers: state loaded and wg0 configured."""
	is_healthy = cluster.is_initialized and peer_state.is_interface_ready()
	code = status.HTTP_204_NO_CONTENT if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE
	return Response(status_code=code)


@app.get("/readyz", tags=["Health"], summary="Check write readiness")
async def readyz() -> Response:
	"""Report whether this node can accept writes. The regional DNS health check uses it."""
	code = status.HTTP_204_NO_CONTENT if cluster.is_ready else status.HTTP_503_SERVICE_UNAVAILABLE
	return Response(status_code=code)


@app.post(
	"/v1/peers",
	tags=["Peers"],
	summary="Register peer",
	responses={
		**ERROR_RESPONSES,
		400: {"description": "A field is out of range or the key is not a WireGuard key."},
		409: {"description": "The peer has another key, or the key belongs to another peer."},
	},
)
async def register_peer(registration: PeerRegistration, authorization: GatewayAuthorization) -> PeerSettings:
	"""Register one peer on the live node with the fewest peers, and return its WireGuard settings."""
	authorization.require("peers", "update")
	validate_device(registration.tenant_id, registration.client_id, registration.public_key)
	identity = get_identity(registration.tenant_id, registration.client_id)

	# Every request goes through the cluster: a retry must also retry a write that missed replication.
	value = {"public_key": registration.public_key}
	result = await cluster.mutate(Mutation(kind=PEERS_KIND, action="update", key=identity, value=value))
	return result.body


@app.delete(
	"/v1/peers",
	status_code=status.HTTP_204_NO_CONTENT,
	tags=["Peers"],
	summary="Remove peer",
	responses=ERROR_RESPONSES,
)
async def remove_peer(peer: PeerIdentity, authorization: GatewayAuthorization) -> Response:
	"""Remove one peer. A missing peer is not an error."""
	authorization.require("peers", "update")
	identity = get_identity(peer.tenant_id, peer.client_id)
	await cluster.mutate(Mutation(kind=PEERS_KIND, action="delete", key=identity))
	return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.put(
	"/v1/peers",
	tags=["Peers"],
	summary="Restore peer table",
	responses={
		**ERROR_RESPONSES,
		400: {"description": "A peer appears twice or names a node that is not a member."},
	},
)
async def replace_peers(table: PeerTable, authorization: GatewayAuthorization) -> TableReplaced:
	"""Replace the complete peer table, such as after a restore."""
	authorization.require("peers", "update")
	value = PeerState.build_table([peer.model_dump() for peer in table.peers])
	result = await cluster.mutate(Mutation(kind=PEERS_KIND, action="replace", value=value))
	return result.body


@app.get("/v1/peers", tags=["Peers"], summary="List peers", responses=ERROR_RESPONSES)
async def list_peers(authorization: GatewayAuthorization) -> list[ListedPeer]:
	"""Return the settings of every registered peer."""
	authorization.require("peers", "read")
	peers = cluster.snapshot.state.get(PEERS_KIND, {})
	return [
		peer_state.get_credentials(identity, peer) | {"node_id": peer["node_id"]}
		for identity, peer in sorted(peers.items())
	]


docs.add_routes(app)
