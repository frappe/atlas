import asyncio
import os
import secrets
import ssl
import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field


class AddressUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(min_length=1)


class CertificateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wildcard_domain: str = Field(min_length=5, max_length=253)
    fullchain_pem: str = Field(min_length=1, max_length=1024 * 1024)
    private_key_pem: str = Field(min_length=1, max_length=1024 * 1024)


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


def _config() -> tuple[str, int, str, Path]:
    host = os.environ.get("ATLAS_CONTROL_HOST", "127.0.0.1")
    port = int(os.environ.get("ATLAS_CONTROL_PORT", "9000"))
    socket_path = os.environ.get("ATLAS_PROXY_ADMIN_SOCKET", "/run/nginx/admin.sock")
    cert_dir = Path(os.environ.get("ATLAS_PROXY_CERT_DIR", "/var/lib/nginx/certs"))
    return host, port, socket_path, cert_dir


_host, _port, _socket_path, _cert_dir = _config()
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


@app.put("/v1/certificate", dependencies=[Depends(authenticated)])
async def update_certificate(update: CertificateUpdate) -> dict[str, str]:
    try:
        region = await asyncio.to_thread(_update_certificate, update)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"updated": True, "region": region}


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


def _update_certificate(update: CertificateUpdate) -> str:
    wildcard = update.wildcard_domain.strip().lower()
    if not wildcard.startswith("*."):
        raise ValueError("wildcard_domain must start with *.")
    region = wildcard[2:]
    if not _valid_region(region):
        raise ValueError("wildcard_domain contains an invalid domain")

    target_dir = _cert_dir / region
    target_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    cert_temp = key_temp = None
    try:
        cert_temp = _write_temp(target_dir, update.fullchain_pem.encode(), 0o644)
        key_temp = _write_temp(target_dir, update.private_key_pem.encode(), 0o640)
        _validate_certificate_pair(cert_temp, key_temp)
        _validate_certificate_name(cert_temp, wildcard)
        _replace_file(cert_temp, target_dir / "fullchain.pem", 0o644)
        cert_temp = None
        _replace_file(key_temp, target_dir / "privkey.pem", 0o640)
        key_temp = None
        _write_region(region)
        _activate_certificates(region)
        _reload_openresty()
    except OSError as error:
        raise RuntimeError("cannot install proxy certificate") from error
    finally:
        for path in (cert_temp, key_temp):
            if path:
                Path(path).unlink(missing_ok=True)
    return region


def _valid_region(region: str) -> bool:
    return (
        region not in {"", ".", ".."}
        and "/" not in region
        and "\\" not in region
        and all(label and label[0].isalnum() and label[-1].isalnum() for label in region.split("."))
    )


def _write_region(region: str) -> None:
    source = _cert_dir.parent / "region"
    temporary = _cert_dir.parent / ".atlas-region.new"
    temporary.write_text(region + "\n")
    os.chmod(temporary, 0o640)
    os.replace(temporary, source)


def _write_temp(directory: Path, content: bytes, mode: int) -> str:
    descriptor, path = tempfile.mkstemp(prefix=".atlas-cert-", dir=directory)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
    except BaseException:
        os.close(descriptor)
        Path(path).unlink(missing_ok=True)
        raise
    return path


def _replace_file(source: str, destination: Path, mode: int) -> None:
    os.chmod(source, mode)
    os.replace(source, destination)


def _activate_certificates(region: str) -> None:
    for name in ("fullchain.pem", "privkey.pem"):
        link = _cert_dir / name
        temporary = _cert_dir / f".{name}.new"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(Path(region) / name)
        os.replace(temporary, link)


def _validate_certificate_pair(cert_path: str, key_path: str) -> None:
    _openssl(cert_path, "x509", "-noout", "-checkend", "0")
    cert = _openssl(cert_path, "x509", "-pubkey", "-noout")
    key = _openssl(key_path, "pkey", "-pubout")
    if cert != key:
        raise ValueError("certificate and private key do not match")


def _validate_certificate_name(cert_path: str, wildcard: str) -> None:
    try:
        certificate = ssl._ssl._test_decode_cert(cert_path)
        names = {
            value.lower().rstrip(".")
            for kind, value in certificate.get("subjectAltName", [])
            if kind == "DNS"
        }
    except (OSError, ValueError) as error:
        raise ValueError("certificate does not cover wildcard_domain") from error
    if wildcard.rstrip(".") not in names:
        raise ValueError("certificate does not cover wildcard_domain")


def _openssl(path: str, kind: str, *arguments: str) -> bytes:
    result = subprocess.run(
        ["openssl", kind, "-in", path, *arguments],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise ValueError(f"invalid {kind} PEM")
    return result.stdout


def _reload_openresty() -> None:
    result = subprocess.run(
        ["/usr/local/openresty/nginx/sbin/nginx", "-t", "-c", "/etc/nginx/nginx.conf"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("OpenResty rejected the new certificate configuration")
    result = subprocess.run(
        ["/usr/local/openresty/nginx/sbin/nginx", "-s", "reload", "-c", "/etc/nginx/nginx.conf"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("OpenResty reload failed")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=_host, port=_port)
