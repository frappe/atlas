"""Mint RS256 JWTs that datum accepts, one per resource_id (a host or a VM).

datum stamps every sample with the token's resource_id and cannot be told otherwise, so a
producer that reports for many resources holds many tokens. Atlas mints them here (signed with
the fleet RSA key in site config `atlas_datum_signing_key`) and ships them to the host's boat
daemon. Keep frappe imports lazy so `encode_token` is testable without a site.
"""

import time

import jwt

# One day. Tokens are re-minted every refresh sweep, so a day comfortably covers a missed one.
DEFAULT_TTL_SECONDS = 24 * 60 * 60


def encode_token(resource_id: str, private_key_pem: str, ttl_seconds: int = DEFAULT_TTL_SECONDS, key_id: str | None = None) -> str:
	"""The pure crypto: an RS256 write-scoped JWT for one resource_id. No frappe."""
	issued_at = int(time.time())
	claims = {
		"resource_id": resource_id,
		"access": ["write"],
		"iat": issued_at,
		"exp": issued_at + ttl_seconds,
	}
	headers = {"kid": key_id} if key_id else None
	return jwt.encode(claims, private_key_pem, algorithm="RS256", headers=headers)


def _signing_config() -> tuple[str, str | None]:
	import frappe

	private_key = frappe.conf.get("atlas_datum_signing_key")
	if not private_key:
		raise RuntimeError("atlas_datum_signing_key is not set in site config; cannot mint a datum token")
	return private_key, frappe.conf.get("atlas_datum_key_id")


def mint(resource_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
	"""Mint a token for an arbitrary resource_id using the configured fleet key."""
	private_key, key_id = _signing_config()
	return encode_token(resource_id, private_key, ttl_seconds, key_id)


def mint_for_server(server_name: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
	"""Host token: resource_id is the Server name."""
	return mint(server_name, ttl_seconds)


def mint_for_vm(vm_name: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
	"""VM token: resource_id is the Virtual Machine name (== its UUID)."""
	return mint(vm_name, ttl_seconds)


def datum_url() -> str | None:
	"""The configured datum base URL, or None if metrics export is not wired."""
	import frappe

	return frappe.conf.get("atlas_datum_url")
