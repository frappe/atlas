"""Create short-lived VM console tokens."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

import frappe
import redis

CONSOLE_TOKEN_TTL_SECONDS = 30
CONSOLE_TOKEN_LENGTH = 256
CONSOLE_TOKEN_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ConsoleConnection:
	"""Store one validated Metal console connection."""

	url: str

	@classmethod
	def from_value(cls, value: object) -> ConsoleConnection:
		"""Return a validated console connection."""
		if not isinstance(value, dict):
			raise ValueError("Console connection must be an object")
		url = value.get("url")
		if not isinstance(url, str) or not cls.is_websocket_url(url):
			raise ValueError("Console connection has an invalid WebSocket URL")
		return cls(url=url)

	@classmethod
	def from_json(cls, value: str | bytes) -> ConsoleConnection:
		"""Decode and validate one stored console connection."""
		return cls.from_value(json.loads(value))

	@staticmethod
	def is_websocket_url(value: str) -> bool:
		"""Return whether a URL identifies a WebSocket server."""
		parsed = urlparse(value)
		return parsed.scheme == "wss" and bool(parsed.netloc)

	def as_dict(self) -> dict[str, str]:
		"""Return values that can be stored as JSON."""
		return asdict(self)


def console_token_key(site: str, token: str) -> str:
	"""Return the Redis key for one site and token."""
	return f"atlas:console:token:{site}:{token}"


def is_valid_console_token(value: object) -> bool:
	"""Return whether a value has the generated console token format."""
	return isinstance(value, str) and len(value) == CONSOLE_TOKEN_LENGTH and value.isalnum()


def issue_console_token(connection: dict[str, Any]) -> str:
	"""Store the console connection under a new token and return the token."""
	validated_connection = ConsoleConnection.from_value(connection)
	payload = json.dumps(validated_connection.as_dict())
	client = redis.from_url(frappe.conf.redis_cache)

	for _ in range(CONSOLE_TOKEN_ATTEMPTS):
		token = frappe.generate_hash(length=CONSOLE_TOKEN_LENGTH)
		if client.set(
			console_token_key(frappe.local.site, token),
			payload,
			ex=CONSOLE_TOKEN_TTL_SECONDS,
			# nx keeps a live session its token
			# so a collision never takes over another console.
			nx=True,
		):
			return token

	raise RuntimeError("Could not generate a console token")
