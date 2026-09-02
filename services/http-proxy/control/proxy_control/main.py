import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .auth import Authentication
from .certificates import CertificateStore
from .client import ProxyClient
from .imds import InstanceMetadata
from .mappings import MappingStore
from .server import run


class AddressUpdate(BaseModel):
	model_config = ConfigDict(extra="forbid")

	address: str = Field(min_length=1)


class CertificateUpdate(BaseModel):
	model_config = ConfigDict(extra="forbid")

	wildcard_domain: str = Field(min_length=5, max_length=253)
	fullchain_pem: str = Field(min_length=1, max_length=1024 * 1024)
	private_key_pem: str = Field(min_length=1, max_length=1024 * 1024)


def _config() -> tuple[int, str, Path, Path, str | None, str | None]:
	port = int(os.environ.get("ATLAS_PROXY_CONTROL_PORT", "9000"))
	socket_path = os.environ.get("ATLAS_PROXY_ADMIN_SOCKET", "/run/nginx/admin.sock")
	cert_dir = Path(os.environ.get("ATLAS_PROXY_CERT_DIR", "/var/lib/nginx/certs"))
	auth_file = Path(os.environ.get("ATLAS_PROXY_CONTROL_AUTH_FILE", "/etc/atlas/proxy-control.htpasswd"))

	metadata = InstanceMetadata()
	jwks_url = os.environ.get("ATLAS_PROXY_CONTROL_JWKS_URL") or metadata.get_user_data("proxy_jwks_url")
	jwks_audience = os.environ.get("ATLAS_PROXY_CONTROL_JWKS_AUDIENCE_ID") or metadata.get_user_data(
		"proxy_jwks_audience_id"
	)

	return port, socket_path, cert_dir, auth_file, jwks_url, jwks_audience


_port, _socket_path, _cert_dir, _auth_file, _jwks_url, _jwks_audience = _config()
auth = Authentication(_auth_file, jwks_url=_jwks_url, jwks_audience=_jwks_audience)
proxy = ProxyClient(_socket_path)
maps = MappingStore(proxy)
certificates = CertificateStore(_cert_dir)


@asynccontextmanager
async def lifespan(_: FastAPI):
	yield
	await proxy.close()


app = FastAPI(title="Atlas proxy control", lifespan=lifespan)
protected = [Depends(auth.require)]


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
	return {"ok": True}


@app.get("/readyz", dependencies=protected)
async def readyz() -> Response:
	try:
		response_status, _ = await proxy.request("GET", "/v1/healthz")
	except httpx.HTTPError:
		return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
	if response_status >= 300:
		return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
	return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/v1/state", dependencies=protected)
async def get_state() -> dict[str, dict[str, str]]:
	sites, domains = await asyncio.gather(maps.get("sites"), maps.get("domains"))
	return {"sites": sites, "domains": domains}


@app.put("/v1/sites", dependencies=protected)
async def replace_sites(values: dict[str, str]) -> dict[str, object]:
	return await maps.replace("sites", values)


@app.put("/v1/domains", dependencies=protected)
async def replace_domains(values: dict[str, str]) -> dict[str, object]:
	return await maps.replace("domains", values)


@app.patch("/v1/{kind}/{key}", dependencies=protected)
async def patch_mapping(kind: str, key: str, value: AddressUpdate) -> dict[str, object]:
	return await maps.update(kind, key, value.address)


@app.delete("/v1/{kind}/{key}", dependencies=protected)
async def delete_mapping(kind: str, key: str) -> Response:
	await maps.delete(kind, key)
	return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.put("/v1/certificate", dependencies=protected)
def update_certificate(update: CertificateUpdate) -> dict[str, str | bool]:
	try:
		region = certificates.install(
			update.wildcard_domain,
			update.fullchain_pem,
			update.private_key_pem,
		)
	except ValueError as error:
		raise HTTPException(status_code=400, detail=str(error)) from error
	except RuntimeError as error:
		raise HTTPException(status_code=503, detail=str(error)) from error
	return {"updated": True, "region": region}


if __name__ == "__main__":
	run(app, _port)
