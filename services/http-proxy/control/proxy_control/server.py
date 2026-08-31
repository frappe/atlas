import asyncio
import socket

import uvicorn
from fastapi import FastAPI


def run(app: FastAPI, port: int) -> None:
    try:
        asyncio.run(_serve(app, port))
    except KeyboardInterrupt:
        pass


async def _serve(app: FastAPI, port: int) -> None:
    listeners = [
        _listener(socket.AF_INET, "0.0.0.0", port),
        _listener(socket.AF_INET6, "::", port),
    ]
    try:
        await uvicorn.Server(uvicorn.Config(app, log_level="info")).serve(sockets=listeners)
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
