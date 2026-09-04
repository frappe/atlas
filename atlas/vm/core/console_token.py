"""Create short-lived VM console tokens."""

from __future__ import annotations

import json

import frappe
import redis

CONSOLE_TOKEN_TTL_SECONDS = 60


def console_token_key(site: str, token: str) -> str:
	return f"atlas:console:token:{site}:{token}"


def issue_console_token(connection: dict[str, str]) -> str:
	"""Store the console connection under a new token and return the token."""
	token = frappe.generate_hash(length=48)
	client = redis.from_url(frappe.conf.redis_cache)
	client.set(
		console_token_key(frappe.local.site, token),
		json.dumps(connection),
		ex=CONSOLE_TOKEN_TTL_SECONDS,
	)
	return token
