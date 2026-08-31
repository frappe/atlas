import asyncio
import socket
from pathlib import Path

import uvicorn
from fastapi import FastAPI


def run(app: FastAPI, port: int, cert_file: Path, key_file: Path) -> None:
    try:
        asyncio.run(_serve(app, port, cert_file, key_file))
    except KeyboardInterrupt:
        pass


async def _serve(app: FastAPI, port: int, cert_file: Path, key_file: Path) -> None:
    listeners = [
        _listener(socket.AF_INET, "0.0.0.0", port),
        _listener(socket.AF_INET6, "::", port),
    ]
    try:
        config = uvicorn.Config(
            app,
            log_level="info",
            ssl_certfile=str(cert_file),
            ssl_keyfile=str(key_file),
        )
        await uvicorn.Server(config).serve(sockets=listeners)
    finally:
        for listener in listeners:
            listener.close()


def _listener(family: socket.AddressFamily, address: str, port: int) -> socket.socket:
    listener = socket.socket(family)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    listener.bind((address, port))
    listener.listen(socket.SOMAXCONN)
    return listener
