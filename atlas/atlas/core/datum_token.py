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


def build_bundle(
	server_name: str,
	vm_names: list[str],
	private_key_pem: str,
	key_id: str | None = None,
	ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict:
	"""The token file's JSON structure: one host token (resource_id = the Server name)
	and one token per VM (resource_id = the VM name). Pure — no frappe — so it is unit-tested."""
	return {
		"host": encode_token(server_name, private_key_pem, ttl_seconds, key_id),
		"vms": {name: encode_token(name, private_key_pem, ttl_seconds, key_id) for name in vm_names},
	}


# The fleet-wide resource_id every host and VM reports under in single-token mode.
# datum stamps each sample with the token's resource_id, so with one token the host and
# all VMs share it; per-VM samples are told apart by a vm=<uuid> label, and host samples
# by a server=<name> label — not by resource_id (see boat internal/metricspush).
FLEET_RESOURCE_ID = "boat"


def fleet_token(ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
	"""The single fleet-wide datum token for single-token (primary) mode. Prefer a static
	token from site config (`atlas_datum_token`) — required when datum verifies against a key
	Atlas does not hold, e.g. a remote datum — otherwise mint one for resource_id="boat" with
	the configured fleet signing key."""
	import frappe

	static = frappe.conf.get("atlas_datum_token")
	if static:
		return static
	return mint(FLEET_RESOURCE_ID, ttl_seconds)


def single_token_bundle(token: str) -> dict:
	"""The single-token file shape: one fleet token and an empty VM map. Every sample lands
	under the token's resource_id; per-VM samples are distinguished by a vm=<uuid> label.
	Pure — no frappe — so it is unit-tested."""
	return {"host": token, "vms": {}}


def single_token_file_json() -> str:
	"""The exact bytes written to /etc/boat/datum-tokens.json in single-token (primary) mode:
	the fleet token (static from site config if provided, else minted)."""
	import json

	return json.dumps(single_token_bundle(fleet_token()))


def token_file_json(server_name: str, vm_names: list[str]) -> str:
	"""The exact bytes written to /etc/boat/datum-tokens.json for one host, signed with the
	configured fleet key."""
	import json

	private_key, key_id = _signing_config()
	return json.dumps(build_bundle(server_name, vm_names, private_key, key_id))


def refresh_all() -> None:
	"""Scheduler entry: re-ship every Active host's single fleet datum token, covering token
	expiry (a minted token is re-minted each sweep; a static token is simply re-installed). A
	no-op when metrics export is not configured."""
	import frappe

	if not frappe.conf.get("atlas_datum_url"):
		return
	for name in frappe.get_all("Server", filters={"status": "Active"}, pluck="name"):
		frappe.enqueue(
			"atlas.atlas.doctype.server.server.refresh_datum_tokens_for_server",
			queue="long",
			job_id=f"datum-tokens-{name}",
			deduplicate=True,
			server_name=name,
		)
