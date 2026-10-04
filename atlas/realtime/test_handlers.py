import asyncio
import base64
import contextlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from atlas.realtime import handlers
from atlas.vm.core.console_token import CONSOLE_TOKEN_LENGTH


class TestConsoleHandlers(IsolatedAsyncioTestCase):
	async def test_open_presents_the_atlas_client_certificate(self) -> None:
		socket = SimpleNamespace(sid="socket-1", site="test.local", emit=AsyncMock())
		connection = {"url": "wss://192.0.2.12:9000/v1/vms/vm-1/console?mode=tty"}
		cache = SimpleNamespace(getdel=AsyncMock(return_value=json.dumps(connection).encode()))
		tls_context = Mock()
		metal_connection = Mock()
		session = Mock()

		with (
			patch.object(handlers, "_cache", return_value=cache),
			patch.object(handlers, "_tls_context", return_value=tls_context) as tls_context_for_site,
			patch.object(handlers.websockets, "connect", AsyncMock(return_value=metal_connection)) as connect,
			patch.object(handlers, "ConsoleSession", return_value=session),
			patch.dict(handlers._sessions, {}, clear=True),
		):
			await handlers.atlas_console_open(socket, "a" * CONSOLE_TOKEN_LENGTH)
			self.assertIs(handlers._sessions[socket.sid], session)

		tls_context_for_site.assert_called_once_with("test.local")
		connect.assert_awaited_once_with(connection["url"], max_size=None, ssl=tls_context)
		socket.emit.assert_awaited_once_with("atlas_console_ready")

	def test_tls_context_loads_the_regional_files(self) -> None:
		context = Mock()

		with (
			patch.object(handlers.frappe, "local", SimpleNamespace(sites_path="/bench/sites")),
			patch.object(handlers.ssl, "create_default_context", return_value=context) as create_context,
		):
			self.assertIs(handlers._tls_context("test.local"), context)

		create_context.assert_called_once_with(
			cafile="/bench/sites/test.local/private/atlas-metal-tls/ca.crt"
		)
		context.load_cert_chain.assert_called_once_with(
			"/bench/sites/test.local/private/atlas-metal-tls/atlas.crt",
			"/bench/sites/test.local/private/atlas-metal-tls/atlas.key",
		)

	def test_the_realtime_server_reads_its_files_without_a_frappe_context(self) -> None:
		context = Mock()
		with tempfile.TemporaryDirectory() as sites_path:
			Path(sites_path, "common_site_config.json").write_text(
				json.dumps({"redis_cache": "redis://cache.test:13000"})
			)
			with (
				contextlib.chdir(sites_path),
				patch.object(handlers.frappe, "local", SimpleNamespace()),
				patch.object(handlers, "_redis_client", None),
				patch.object(handlers.ssl, "create_default_context", return_value=context) as create_context,
			):
				cache = handlers._cache()
				handlers._tls_context("test.local")

		self.assertEqual(cache.connection_pool.connection_kwargs["host"], "cache.test")
		create_context.assert_called_once_with(cafile="test.local/private/atlas-metal-tls/ca.crt")

	async def test_open_rejects_an_invalid_stored_payload(self) -> None:
		socket = SimpleNamespace(sid="socket-1", site="test.local", emit=AsyncMock())
		cache = SimpleNamespace(getdel=AsyncMock(return_value=b"not-json"))

		with (
			patch.object(handlers, "_cache", return_value=cache),
			patch.object(handlers.frappe, "log_error") as log_error,
			patch.dict(handlers._sessions, {}, clear=True),
		):
			await handlers.atlas_console_open(socket, "a" * CONSOLE_TOKEN_LENGTH)

		log_error.assert_called_once_with(title="Invalid console token payload for site test.local")
		socket.emit.assert_awaited_once_with(
			"atlas_console_error", "This console link is invalid or expired."
		)

	async def test_input_rejects_invalid_base64(self) -> None:
		socket = SimpleNamespace(sid="socket-1", emit=AsyncMock())
		session = SimpleNamespace(send_input=AsyncMock())

		with patch.dict(handlers._sessions, {"socket-1": session}, clear=True):
			await handlers.atlas_console_input(socket, "not base64!")

		session.send_input.assert_not_awaited()
		socket.emit.assert_awaited_once_with("atlas_console_error", "Console input is invalid.")

	async def test_input_rejects_an_oversized_payload(self) -> None:
		socket = SimpleNamespace(sid="socket-1", emit=AsyncMock())
		session = SimpleNamespace(send_input=AsyncMock())
		data = base64.b64encode(b"a" * (handlers.MAXIMUM_CONSOLE_INPUT_BYTES + 1)).decode()

		with patch.dict(handlers._sessions, {"socket-1": session}, clear=True):
			await handlers.atlas_console_input(socket, data)

		session.send_input.assert_not_awaited()
		socket.emit.assert_awaited_once_with("atlas_console_error", "Console input is too large.")

	async def test_session_owns_idempotent_cleanup(self) -> None:
		socket = SimpleNamespace(sid="socket-1", emit=AsyncMock())
		connection = SimpleNamespace(close=AsyncMock())
		stream_task = asyncio.create_task(asyncio.Event().wait())
		session = object.__new__(handlers.ConsoleSession)
		session.socket = socket
		session.connection = connection
		session.is_closed = False
		session.stream_task = stream_task

		with patch.dict(handlers._sessions, {"socket-1": session}, clear=True):
			await session.close()
			await session.close()
			self.assertNotIn("socket-1", handlers._sessions)

		self.assertTrue(stream_task.cancelled())
		connection.close.assert_awaited_once()
		socket.emit.assert_awaited_once_with("atlas_console_closed")
