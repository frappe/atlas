from typing import Any

from fastapi import HTTPException

from .client import ProxyClient


class MappingStore:
    """Read and change site and custom-domain maps in OpenResty."""

    def __init__(self, client: ProxyClient):
        self.client = client

    async def get(self, kind: str) -> dict[str, str]:
        self._validate_kind(kind)
        status, body = await self.client.request("GET", f"/v1/{kind}")
        self._check_response(status, body)
        if not self._is_map(body):
            raise HTTPException(status_code=502, detail="proxy returned an invalid map")
        return body

    async def replace(self, kind: str, values: dict[str, str]) -> dict[str, Any]:
        self._validate_kind(kind)
        return await self._forward("PUT", f"/v1/{kind}", values)

    async def update(self, kind: str, key: str, address: str) -> dict[str, Any]:
        self._validate_kind(kind)
        return await self._forward("PATCH", f"/v1/{kind}/{key}", {"address": address})

    async def delete(self, kind: str, key: str) -> None:
        self._validate_kind(kind)
        await self._forward("DELETE", f"/v1/{kind}/{key}")

    async def _forward(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        status, response_body = await self.client.request(method, path, body)
        self._check_response(status, response_body)
        return response_body if isinstance(response_body, dict) else {}

    def _validate_kind(self, kind: str) -> None:
        if kind not in {"sites", "domains"}:
            raise HTTPException(status_code=404, detail="mapping type not found")

    def _is_map(self, body: Any) -> bool:
        return isinstance(body, dict) and all(
            isinstance(key, str) and isinstance(address, str)
            for key, address in body.items()
        )

    def _check_response(self, status: int, body: Any) -> None:
        if status >= 300:
            raise HTTPException(
                status_code=502,
                detail={"proxy_status": status, "proxy": body},
            )
