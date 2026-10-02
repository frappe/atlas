from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from atlas_control.auth import Authentication, Authorization
from atlas_control.cluster import ClusterManager, Mutation
from atlas_control.routes import create_cluster_router
from fastapi import Body, Depends, FastAPI, Path, Response, status
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from . import docs
from .client import ProxyClient
from .config import ConfigError, load, load_auth
from .mappings import MappingStore
from .routes import RouteState
from .server import run

SCOPES = frozenset({"*", "site:*", "domain:*"})
CONSTRAINED_RESOURCES = frozenset({"site", "domain"})
SITE_MAP_EXAMPLE = {"erp": "2001:db8::10", "shop": "2001:db8::11"}
DOMAIN_MAP_EXAMPLE = {"www.example.com": "2001:db8::20"}


class AddressUpdate(BaseModel):
	"""One address for one site or custom domain."""

	model_config = ConfigDict(extra="forbid")

	address: str = Field(
		min_length=1,
		examples=["2001:db8::10"],
		description="The backend IPv6 address. Use `-` only for a site to stop its traffic.",
	)


class MapReplaced(BaseModel):
	"""Result of a full map replacement."""

	synced: bool = Field(description="Whether the complete map was applied.")
	entries: int = Field(description="Number of entries in the replacement map.")


class SiteMapping(BaseModel):
	"""One site as the proxy stored it."""

	site: str = Field(description="The site subdomain.")
	address: str = Field(description="The backend IPv6 address.")


class DomainMapping(BaseModel):
	"""One custom domain as the proxy stored it."""

	domain: str = Field(description="The custom domain.")
	address: str = Field(description="The backend IPv6 address.")


try:
	_config = load()
except ConfigError as error:
	raise SystemExit(f"atlas-proxy-control: {error}") from error


auth = Authentication(load_auth, SCOPES, "atlas-proxy-control", CONSTRAINED_RESOURCES)
proxy = ProxyClient(_config.admin_socket)
maps = MappingStore(
	proxy,
	_config.reserved_subdomains,
	_config.tls.wildcard_domain,
	_config.auto_proxy_host_prefixes,
)
route_state = RouteState(maps)
cluster = ClusterManager(_config.cluster, route_state)


@asynccontextmanager
async def lifespan(_: FastAPI):
	await cluster.start()
	try:
		yield
	finally:
		await cluster.close()
		await proxy.close()


def operation_id(route: APIRoute) -> str:
	"""Return the route function name as the OpenAPI operation ID."""
	return route.name


app = FastAPI(
	title="Atlas proxy control",
	description="Route sites and custom domains to backend IPv6 addresses. Sync all routes after a controller restart, or change one route when an address changes.",
	lifespan=lifespan,
	generate_unique_id_function=operation_id,
	docs_url=None,
	redoc_url=None,
	openapi_url=None,
	openapi_tags=[
		{"name": "Health", "description": "Use these routes for liveness and readiness checks."},
		{"name": "Sites", "description": "Route wildcard subdomains to backend IPv6 addresses."},
		{"name": "Domains", "description": "Route custom domains to backend IPv6 addresses."},
	],
)
ControlAuthorization = Annotated[Authorization, Depends(auth.require_request)]
app.include_router(create_cluster_router(cluster))


@app.get(
	"/healthz",
	tags=["Health"],
	summary="Check traffic health",
	description="Use this route for the DNS health check. It checks OpenResty and the routes it holds.",
)
async def healthz() -> Response:
	if not cluster.is_initialized:
		return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
	try:
		response_status, counts = await proxy.request("GET", "/v1/healthz")
	except httpx.HTTPError:
		return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
	if response_status >= 300 or not isinstance(counts, dict):
		return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
	if not route_state.has_routes(cluster.snapshot.state, counts):
		return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
	return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
	"/readyz",
	tags=["Health"],
	summary="Check OpenResty readiness",
	description="Use this route before a controller sends routing updates. It checks the OpenResty admin API.",
)
async def readyz() -> Response:
	if not cluster.is_ready:
		return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
	try:
		response_status, _ = await proxy.request("GET", "/v1/healthz")
	except httpx.HTTPError:
		return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
	if response_status >= 300:
		return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
	return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
	"/v1/sites",
	tags=["Sites"],
	summary="List site routes",
	description="Read site routes during reconciliation.",
)
async def get_sites(authorization: ControlAuthorization) -> dict[str, str]:
	return authorization.filter("site", await maps.get("sites"))


@app.put(
	"/v1/sites",
	tags=["Sites"],
	summary="Sync site routes",
	description="Replace every site route after a controller restart or full reconciliation. Example: `erp` routes to `2001:db8::10`.",
)
async def replace_sites(
	response: Response,
	values: Annotated[
		dict[str, str],
		Body(examples=[SITE_MAP_EXAMPLE], description="The complete desired site map."),
	],
	authorization: ControlAuthorization,
) -> MapReplaced:
	authorization.require_unconstrained("site", "update")
	result = await cluster.mutate(Mutation(kind="sites", action="replace", value=values))
	response.headers["X-Atlas-Proxy-Generation"] = str(result.generation)
	return MapReplaced(**result.body)


@app.get(
	"/v1/domains",
	tags=["Domains"],
	summary="List domain routes",
	description="Read custom-domain routes during reconciliation.",
)
async def get_domains(authorization: ControlAuthorization) -> dict[str, str]:
	return authorization.filter("domain", await maps.get("domains"))


@app.put(
	"/v1/domains",
	tags=["Domains"],
	summary="Sync domain routes",
	description="Replace every custom-domain route after a controller restart or full reconciliation. Example: `www.example.com` routes to `2001:db8::20`.",
)
async def replace_domains(
	response: Response,
	values: Annotated[
		dict[str, str],
		Body(examples=[DOMAIN_MAP_EXAMPLE], description="The complete desired custom-domain map."),
	],
	authorization: ControlAuthorization,
) -> MapReplaced:
	authorization.require_unconstrained("domain", "update")
	result = await cluster.mutate(Mutation(kind="domains", action="replace", value=values))
	response.headers["X-Atlas-Proxy-Generation"] = str(result.generation)
	return MapReplaced(**result.body)


@app.patch(
	"/v1/sites/{name}",
	tags=["Sites"],
	summary="Update site route",
	description="Add or change one site route without changing other sites. Example: route `erp` to `2001:db8::10`.",
)
async def patch_site(
	response: Response,
	name: Annotated[str, Path(description="The site subdomain.")],
	value: AddressUpdate,
	authorization: ControlAuthorization,
) -> SiteMapping:
	authorization.require("site", "update", name)
	result = await cluster.mutate(Mutation(kind="sites", action="update", key=name, value=value.address))
	response.headers["X-Atlas-Proxy-Generation"] = str(result.generation)
	return SiteMapping(**result.body)


@app.delete(
	"/v1/sites/{name}",
	tags=["Sites"],
	status_code=status.HTTP_204_NO_CONTENT,
	summary="Remove site route",
	description="Remove one site route when it no longer needs proxy traffic. This succeeds when the site is absent.",
)
async def delete_site(
	name: Annotated[str, Path(description="The site subdomain.")],
	authorization: ControlAuthorization,
) -> Response:
	authorization.require("site", "delete", name)
	result = await cluster.mutate(Mutation(kind="sites", action="delete", key=name))
	return Response(
		status_code=status.HTTP_204_NO_CONTENT,
		headers={"X-Atlas-Proxy-Generation": str(result.generation)},
	)


@app.patch(
	"/v1/domains/{domain}",
	tags=["Domains"],
	summary="Update domain route",
	description="Add or change one custom-domain route without changing other domains. Example: route `www.example.com` to `2001:db8::20`.",
)
async def patch_domain(
	response: Response,
	domain: Annotated[str, Path(description="The complete custom domain.")],
	value: AddressUpdate,
	authorization: ControlAuthorization,
) -> DomainMapping:
	authorization.require("domain", "update", domain)
	result = await cluster.mutate(Mutation(kind="domains", action="update", key=domain, value=value.address))
	response.headers["X-Atlas-Proxy-Generation"] = str(result.generation)
	return DomainMapping(**result.body)


@app.delete(
	"/v1/domains/{domain}",
	tags=["Domains"],
	status_code=status.HTTP_204_NO_CONTENT,
	summary="Remove domain route",
	description="Remove one custom-domain route when it no longer needs proxy traffic. This succeeds when the domain is absent.",
)
async def delete_domain(
	domain: Annotated[str, Path(description="The complete custom domain.")],
	authorization: ControlAuthorization,
) -> Response:
	authorization.require("domain", "delete", domain)
	result = await cluster.mutate(Mutation(kind="domains", action="delete", key=domain))
	return Response(
		status_code=status.HTTP_204_NO_CONTENT,
		headers={"X-Atlas-Proxy-Generation": str(result.generation)},
	)


@app.get("/v1/cluster/status", tags=["Health"])
async def cluster_status(authorization: ControlAuthorization) -> dict[str, object]:
	"""Return the local cluster status."""
	authorization.require("cluster", "read")
	return cluster.status()


docs.add_routes(app)


if __name__ == "__main__":
	run(app)
