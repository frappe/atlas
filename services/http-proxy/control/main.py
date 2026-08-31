import asyncio
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
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


class ControlState:
    def __init__(self, path: str):
        self.path = Path(path)
        self.sites: dict[str, str] = {}
        self.domains: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.last_reconcile: float | None = None
        self.last_error: str | None = None
        self.proxy_ok = False
        self.proxy_boot_id: str | None = None
        self.save_task: asyncio.Task[None] | None = None

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        if not isinstance(data, dict):
            raise ValueError("control state must be an object")
        self.sites = _mapping(data.get("sites", {}))
        self.domains = _mapping(data.get("domains", {}))

    def save(self) -> None:
        # Rename makes a restart see either the old complete file or the new one.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"sites": self.sites, "domains": self.domains}, sort_keys=True, indent=2)
            + "\n"
        )
        os.replace(temporary, self.path)

    def schedule_save(self) -> None:
        if self.save_task is None or self.save_task.done():
            self.save_task = asyncio.create_task(self._save_after_delay())

    async def _save_after_delay(self) -> None:
        await asyncio.sleep(1)
        async with self.lock:
            self.save()

    async def flush_save(self) -> None:
        if self.save_task is not None:
            await self.save_task
            self.save_task = None


def _mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(address, str)
        for key, address in value.items()
    ):
        raise ValueError("mapping must contain string keys and values")
    return value


def _config() -> tuple[str, int, str, str]:
    token = os.environ.get("ATLAS_CONTROL_TOKEN")
    if not token:
        raise RuntimeError("ATLAS_CONTROL_TOKEN is required")
    host = os.environ.get("ATLAS_CONTROL_HOST", "127.0.0.1")
    port = int(os.environ.get("ATLAS_CONTROL_PORT", "9000"))
    socket_path = os.environ.get("ATLAS_PROXY_ADMIN_SOCKET", "/run/nginx/admin.sock")
    state_path = os.environ.get("ATLAS_CONTROL_STATE", "/var/lib/nginx/control-state.json")
    return host, port, socket_path, state_path


_host, _port, _socket_path, _state_path = _config()
state = ControlState(_state_path)
proxy = ProxyClient(_socket_path)


async def reconcile() -> None:
    async with state.lock:
        health = await _proxy_health()
        boot_id = health["boot_id"]
        if state.proxy_ok and state.proxy_boot_id == boot_id:
            return
        await _sync_maps({"sites": state.sites, "domains": state.domains})
        state.proxy_boot_id = boot_id
        state.last_reconcile = time.time()
        state.last_error = None
        state.proxy_ok = True


async def reconcile_loop() -> None:
    while True:
        try:
            await reconcile()
        except Exception as error:
            state.proxy_ok = False
            state.last_error = str(error)
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.load()
    task = asyncio.create_task(reconcile_loop())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await state.flush_save()
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
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "proxy_ok": state.proxy_ok,
        "last_reconcile": state.last_reconcile,
        "error": state.last_error,
    }


@app.get("/readyz")
async def readyz() -> Response:
    if state.proxy_ok:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)


@app.get("/v1/state", dependencies=[Depends(authenticated)])
async def get_state() -> dict[str, dict[str, str]]:
    return {"sites": state.sites, "domains": state.domains}


@app.put("/v1/sites", dependencies=[Depends(authenticated)])
async def replace_sites(values: dict[str, str]) -> dict[str, Any]:
    return await _replace("sites", values)


@app.put("/v1/domains", dependencies=[Depends(authenticated)])
async def replace_domains(values: dict[str, str]) -> dict[str, Any]:
    return await _replace("domains", values)


@app.patch("/v1/sites/{key}", dependencies=[Depends(authenticated)])
async def patch_site(key: str, value: AddressUpdate) -> dict[str, str]:
    return await _patch("sites", key, value.address)


@app.patch("/v1/domains/{key}", dependencies=[Depends(authenticated)])
async def patch_domain(key: str, value: AddressUpdate) -> dict[str, str]:
    return await _patch("domains", key, value.address)


@app.delete("/v1/sites/{key}", dependencies=[Depends(authenticated)])
async def delete_site(key: str) -> Response:
    await _delete("sites", key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.delete("/v1/domains/{key}", dependencies=[Depends(authenticated)])
async def delete_domain(key: str) -> Response:
    await _delete("domains", key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _replace(kind: str, values: dict[str, str]) -> dict[str, Any]:
    mapping = _mapping(values)
    async with state.lock:
        candidate = {"sites": state.sites, "domains": state.domains}
        candidate[kind] = mapping
        await _sync(candidate)
        setattr(state, kind, mapping)
        state.schedule_save()
    return {"synced": True, "entries": len(mapping)}


async def _patch(kind: str, key: str, address: str) -> dict[str, str]:
    async with state.lock:
        path = f"/v1/{kind}/{key}"
        response_status, body = await proxy.request(
            "PATCH", path, {"address": address}
        )
        _check_proxy_response(response_status, body)
        mapping = dict(getattr(state, kind))
        mapping[key] = address
        setattr(state, kind, mapping)
        state.schedule_save()
    return {kind[:-1]: key, "address": address}


async def _delete(kind: str, key: str) -> None:
    async with state.lock:
        path = f"/v1/{kind}/{key}"
        response_status, body = await proxy.request("DELETE", path)
        _check_proxy_response(response_status, body)
        mapping = dict(getattr(state, kind))
        mapping.pop(key, None)
        setattr(state, kind, mapping)
        state.schedule_save()


async def _sync(maps: dict[str, dict[str, str]]) -> None:
    health = await _proxy_health()
    await _sync_maps(maps)
    state.proxy_boot_id = health["boot_id"]
    state.proxy_ok = True
    state.last_error = None


async def _proxy_health() -> dict[str, Any]:
    health_status, health_body = await proxy.request("GET", "/v1/healthz")
    _check_proxy_response(health_status, health_body)
    if not isinstance(health_body, dict) or not isinstance(health_body.get("boot_id"), str):
        raise RuntimeError("proxy health response has no boot_id")
    return health_body


async def _sync_maps(maps: dict[str, dict[str, str]]) -> None:
    for kind in ("sites", "domains"):
        path = f"/v1/{kind}/sync"
        response_status, body = await proxy.request("POST", path, maps[kind])
        _check_proxy_response(response_status, body)


def _check_proxy_response(response_status: int, body: Any) -> None:
    if response_status >= 300:
        raise HTTPException(
            status_code=502,
            detail={"proxy_status": response_status, "proxy": body},
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=_host, port=_port)
