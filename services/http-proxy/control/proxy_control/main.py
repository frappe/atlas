import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import bcrypt
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from .certificates import CertificateStore
from .client import ProxyClient
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


class Authentication:
    """Check bearer passwords against the configured htpasswd file."""

    def __init__(self, path: Path):
        self.path = path

    def require(self, authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, password = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not self._matches(password):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    def _matches(self, password: str) -> bool:
        try:
            line = next(
                line for line in self.path.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
            _, separator, password_hash = line.partition(":")
            if not separator or not password_hash:
                return False
            return bcrypt.checkpw(password.encode(), password_hash.encode())
        except (OSError, ValueError, StopIteration):
            return False


def _config() -> tuple[int, str, Path, Path, Path, Path]:
    port = int(os.environ.get("ATLAS_CONTROL_PORT", "9000"))
    socket_path = os.environ.get("ATLAS_PROXY_ADMIN_SOCKET", "/run/nginx/admin.sock")
    cert_dir = Path(os.environ.get("ATLAS_PROXY_CERT_DIR", "/var/lib/nginx/certs"))
    auth_file = Path(
        os.environ.get("ATLAS_CONTROL_AUTH_FILE", "/etc/atlas/proxy-control.htpasswd")
    )
    cert_file = Path(
        os.environ.get("ATLAS_CONTROL_TLS_CERT_FILE", "/var/lib/nginx/certs/fullchain.pem")
    )
    key_file = Path(
        os.environ.get("ATLAS_CONTROL_TLS_KEY_FILE", "/var/lib/nginx/certs/privkey.pem")
    )
    return port, socket_path, cert_dir, auth_file, cert_file, key_file


_port, _socket_path, _cert_dir, _auth_file, _cert_file, _key_file = _config()
auth = Authentication(_auth_file)
proxy = ProxyClient(_socket_path)
maps = MappingStore(proxy)
certificates = CertificateStore(_cert_dir)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await proxy.close()


app = FastAPI(title="Atlas proxy control", lifespan=lifespan)
protected = [Depends(auth.require)]


@app.get("/healthz", dependencies=protected)
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
    run(app, _port, _cert_file, _key_file)
