"""Bridge browser console sessions to Metal."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json

import frappe
import redis.asyncio as redis
import websockets
from frappe.realtime import Socket, realtime

from atlas.vm.core.console_token import console_token_key

# Active bridges by socket ID.
_sessions: dict[str, "ConsoleSession"] = {}

_redis_client: redis.Redis | None = None


def _cache() -> redis.Redis:
	"""Return the shared Redis cache client."""
	global _redis_client
	if _redis_client is None:
		url = frappe.get_common_site_config(sites_path=frappe.local.sites_path)["redis_cache"]
		_redis_client = redis.from_url(url)
	return _redis_client


class ConsoleSession:
	"""Bridge one browser to one Metal console."""

	def __init__(self, socket: Socket, connection: websockets.ClientConnection):
		self.socket = socket
		self.connection = connection
		self.stream_task = asyncio.create_task(self.stream_output())

	async def stream_output(self) -> None:
		"""Forward console output until either side closes, then clean up the session."""
		try:
			async for message in self.connection:
				data = message if isinstance(message, bytes) else message.encode()
				await self.socket.emit("atlas_console_output", base64.b64encode(data).decode())
		except (websockets.WebSocketException, asyncio.CancelledError):
			pass
		finally:
			# The Metal side may close first, so remove this session here instead of
			# relying only on the browser disconnect.
			if _sessions.get(self.socket.sid) is self:
				del _sessions[self.socket.sid]
			with contextlib.suppress(Exception):
				await self.connection.close()
			with contextlib.suppress(Exception):
				await self.socket.emit("atlas_console_closed")

	async def send_input(self, data: bytes) -> None:
		await self.connection.send(data)

	async def send_resize(self, cols: int, rows: int) -> None:
		await self.connection.send(json.dumps({"resize": {"cols": cols, "rows": rows}}))

	async def close(self) -> None:
		self.stream_task.cancel()
		await self.connection.close()


@realtime.on("atlas_console_open", allow_guest=True)
async def atlas_console_open(socket: Socket, token: str) -> None:
	"""Consume the token and open the console."""
	if socket.sid in _sessions:
		return

	raw = await _cache().getdel(console_token_key(socket.site, token))
	if not raw:
		await socket.emit("atlas_console_error", "This console link is invalid or expired.")
		return

	connection = json.loads(raw)
	try:
		metal_connection = await websockets.connect(
			connection["url"],
			additional_headers={"Authorization": connection["authorization"]},
			max_size=None,
		)
	except (OSError, websockets.WebSocketException):
		await socket.emit("atlas_console_error", "Could not reach the virtual machine console.")
		return

	_sessions[socket.sid] = ConsoleSession(socket, metal_connection)
	await socket.emit("atlas_console_ready")


@realtime.on("atlas_console_input", allow_guest=True)
async def atlas_console_input(socket: Socket, data: str) -> None:
	session = _sessions.get(socket.sid)
	if session:
		await session.send_input(base64.b64decode(data))


@realtime.on("atlas_console_resize", allow_guest=True)
async def atlas_console_resize(socket: Socket, size: dict) -> None:
	session = _sessions.get(socket.sid)
	if session and isinstance(size, dict):
		await session.send_resize(
			_terminal_dimension(size.get("cols"), 80), _terminal_dimension(size.get("rows"), 24)
		)


def _terminal_dimension(value: object, default: int) -> int:
	"""Clamp a viewer terminal size to a sane range."""
	if not isinstance(value, int) or isinstance(value, bool):
		return default
	return max(1, min(value, 1000))


@realtime.on("disconnect", allow_guest=True)
async def atlas_console_disconnect(socket: Socket) -> None:
	session = _sessions.pop(socket.sid, None)
	if session:
		await session.close()
