import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field


class AddressUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(min_length=1)


class ProxyClient:
    def __init__(self, socket_path: str):
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://localhost",
            timeout=5,
        )

    async def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        response = await self.client.request(method, path, json=body)
        if not response.content:
            return response.status_code, None
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    async def close(self) -> None:
        await self.client.aclose()


def _config() -> tuple[str, int, str]:
    host = os.environ.get("ATLAS_CONTROL_HOST", "127.0.0.1")
    port = int(os.environ.get("ATLAS_CONTROL_PORT", "9000"))
    socket_path = os.environ.get("ATLAS_PROXY_ADMIN_SOCKET", "/run/nginx/admin.sock")
    return host, port, socket_path


_host, _port, _socket_path = _config()
proxy = ProxyClient(_socket_path)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not os.environ.get("ATLAS_CONTROL_TOKEN"):
        raise RuntimeError("ATLAS_CONTROL_TOKEN is required")
    yield
    await proxy.close()


app = FastAPI(title="Atlas proxy control", lifespan=lifespan)


async def authenticated(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = os.environ["ATLAS_CONTROL_TOKEN"]
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/readyz")
async def readyz() -> Response:
    try:
        response_status, _ = await proxy.request("GET", "/v1/healthz")
    except Exception:
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    if response_status >= 300:
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/v1/state", dependencies=[Depends(authenticated)])
async def get_state() -> dict[str, dict[str, str]]:
    sites, domains = await asyncio.gather(_get_map("sites"), _get_map("domains"))
    return {"sites": sites, "domains": domains}


@app.put("/v1/sites", dependencies=[Depends(authenticated)])
async def replace_sites(values: dict[str, str]) -> dict[str, Any]:
    return await _replace("sites", values)


@app.put("/v1/domains", dependencies=[Depends(authenticated)])
async def replace_domains(values: dict[str, str]) -> dict[str, Any]:
    return await _replace("domains", values)


@app.patch("/v1/sites/{key}", dependencies=[Depends(authenticated)])
async def patch_site(key: str, value: AddressUpdate) -> dict[str, Any]:
    return await _forward("PATCH", f"/v1/sites/{key}", {"address": value.address})


@app.patch("/v1/domains/{key}", dependencies=[Depends(authenticated)])
async def patch_domain(key: str, value: AddressUpdate) -> dict[str, Any]:
    return await _forward("PATCH", f"/v1/domains/{key}", {"address": value.address})


@app.delete("/v1/sites/{key}", dependencies=[Depends(authenticated)])
async def delete_site(key: str) -> Response:
    await _forward("DELETE", f"/v1/sites/{key}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.delete("/v1/domains/{key}", dependencies=[Depends(authenticated)])
async def delete_domain(key: str) -> Response:
    await _forward("DELETE", f"/v1/domains/{key}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _get_map(kind: str) -> dict[str, str]:
    response_status, body = await proxy.request("GET", f"/v1/{kind}")
    _check_proxy_response(response_status, body)
    if not isinstance(body, dict) or not all(
        isinstance(key, str) and isinstance(address, str)
        for key, address in body.items()
    ):
        raise HTTPException(status_code=502, detail="proxy returned an invalid map")
    return body


async def _replace(kind: str, values: dict[str, str]) -> dict[str, Any]:
    return await _forward("PUT", f"/v1/{kind}", values)


async def _forward(method: str, path: str, body: Any = None) -> dict[str, Any]:
    response_status, response_body = await proxy.request(method, path, body)
    _check_proxy_response(response_status, response_body)
    if isinstance(response_body, dict):
        return response_body
    return {}


def _check_proxy_response(response_status: int, body: Any) -> None:
    if response_status >= 300:
        raise HTTPException(
            status_code=502,
            detail={"proxy_status": response_status, "proxy": body},
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=_host, port=_port)
