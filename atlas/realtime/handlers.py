"""Bridge browser console sessions to Metal."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import ssl
from pathlib import Path

import frappe
import redis.asyncio as redis
import websockets
from frappe.realtime import Socket, realtime

from atlas.atlas.core.tls.metal import TLS_DIRECTORY
from atlas.vm.core.console_token import ConsoleConnection, console_token_key, is_valid_console_token

# Active bridges by socket ID.
_sessions: dict[str, "ConsoleSession"] = {}

_redis_client: redis.Redis | None = None
MAXIMUM_CONSOLE_INPUT_BYTES = 64 * 1024


def _tls_context(site: str) -> ssl.SSLContext:
	"""Return the client context that authenticates Atlas to Metal."""
	directory = Path(frappe.local.sites_path, site, *TLS_DIRECTORY)
	context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
	context.load_cert_chain(str(directory / "atlas.crt"), str(directory / "atlas.key"))
	return context


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
		self.is_closed = False
		self.stream_task = asyncio.create_task(self.stream_output())

	async def stream_output(self) -> None:
		"""Forward console output until either side closes, then clean up the session."""
		try:
			async for message in self.connection:
				data = message if isinstance(message, bytes) else message.encode()
				await self.socket.emit("atlas_console_output", base64.b64encode(data).decode())
		except websockets.WebSocketException, asyncio.CancelledError:
			pass
		except Exception:
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"Console stream failed for socket {self.socket.sid}",
			)
		finally:
			await self.close()

	async def send_input(self, data: bytes) -> None:
		"""Send one input frame to Metal."""
		await self.connection.send(data)

	async def send_resize(self, cols: int, rows: int) -> None:
		"""Send one resize control message to Metal."""
		await self.connection.send(json.dumps({"resize": {"cols": cols, "rows": rows}}))

	async def close(self) -> None:
		"""Close this session once and remove it from the active session map."""
		if self.is_closed:
			return
		self.is_closed = True
		if _sessions.get(self.socket.sid) is self:
			del _sessions[self.socket.sid]

		if asyncio.current_task() is not self.stream_task:
			self.stream_task.cancel()
			try:
				await self.stream_task
			except asyncio.CancelledError:
				pass

		try:
			await self.connection.close()
		except OSError, websockets.WebSocketException:
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"Could not close console connection for socket {self.socket.sid}",
			)
		try:
			await self.socket.emit("atlas_console_closed")
		except Exception:
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"Could not notify console closure for socket {self.socket.sid}",
			)


@realtime.on("atlas_console_open", allow_guest=True)
async def atlas_console_open(socket: Socket, token: str) -> None:
	"""Consume the token and open the console."""
	if socket.sid in _sessions:
		return
	if not is_valid_console_token(token):
		await socket.emit("atlas_console_error", "This console link is invalid or expired.")
		return

	# Claim the slot before the first await so a concurrent open cannot leak a second connection.
	_sessions[socket.sid] = None
	try:
		serialized_connection = await _cache().getdel(console_token_key(socket.site, token))
		if not serialized_connection:
			await socket.emit("atlas_console_error", "This console link is invalid or expired.")
			return

		try:
			connection = ConsoleConnection.from_json(serialized_connection)
		except json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError:
			frappe.log_error(title=f"Invalid console token payload for site {socket.site}")
			await socket.emit("atlas_console_error", "This console link is invalid or expired.")
			return
		try:
			metal_connection = await websockets.connect(
				connection.url, max_size=None, ssl=_tls_context(socket.site)
			)
		except OSError, websockets.WebSocketException:
			await socket.emit("atlas_console_error", "Could not reach the virtual machine console.")
			return

		_sessions[socket.sid] = ConsoleSession(socket, metal_connection)
		await socket.emit("atlas_console_ready")
	finally:
		if _sessions.get(socket.sid) is None:
			_sessions.pop(socket.sid, None)


@realtime.on("atlas_console_input", allow_guest=True)
async def atlas_console_input(socket: Socket, data: str) -> None:
	"""Forward viewer keystrokes to the console session."""
	session = _sessions.get(socket.sid)
	if not session:
		return
	if not isinstance(data, str):
		await socket.emit("atlas_console_error", "Console input is invalid.")
		return
	try:
		decoded_data = base64.b64decode(data, validate=True)
	except binascii.Error, ValueError:
		await socket.emit("atlas_console_error", "Console input is invalid.")
		return
	if len(decoded_data) > MAXIMUM_CONSOLE_INPUT_BYTES:
		await socket.emit("atlas_console_error", "Console input is too large.")
		return
	await session.send_input(decoded_data)


@realtime.on("atlas_console_resize", allow_guest=True)
async def atlas_console_resize(socket: Socket, size: dict) -> None:
	"""Forward a terminal resize to the console session."""
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
	"""Close the console session for this socket."""
	session = _sessions.pop(socket.sid, None)
	if session:
		await session.close()
