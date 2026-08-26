"""Central API client.

Central is the global control plane (spec/16-central.md). One Central manages
many Atlas instances; Atlas is the *client*. This is the inverse of the Provider
relationship — so the client mirrors atlas/atlas/digitalocean.py: a thin
requests wrapper, one *Error type, dataclasses for the typed responses.

Atlas calls Central's whitelisted methods at `<url>/api/method/central.api.atlas.<name>`
with a `token <api_key>:<api_secret>` header (spec/16-central.md § "The wire
contract"). Registration is **Central-initiated** now (spec/21-tunnel.md): Central drives the
tunnel handshake and pushes the per-Atlas service-user creds into `Central Settings`
via `provision_tunnel`. Atlas no longer calls
`register`; it only reports outward:

- **ping** — `central.api.atlas.ping` returns `{label}`; a credential + reachability
  check for the Test Connection toast. Stays on the plain admin token.
- **event** — `central.api.atlas.event` (via `post_event`) carries VM lifecycle
  events, signed with HMAC-SHA256 over `X-Atlas-Timestamp + raw body` keyed on the
  per-region `webhook_secret`, and carrying no `Authorization` header at all.

The route names and payloads are the single external dependency; the whole
contract is absorbed here, so a change on Central's side is a one-file edit.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json

import frappe
import requests

DEFAULT_TIMEOUT = 30

# Central method routes. Pinned in one place — the wire contract from
# spec/16-central.md § "The wire contract".
_ROUTES = {
	"ping": "central.api.atlas.ping",
	"event": "central.api.atlas.event",
}


class CentralError(Exception):
	# status_code is the HTTP status when Central answered with one (>=400);
	# None for a network-level failure where no response arrived.
	def __init__(self, message: str, status_code: int | None = None) -> None:
		super().__init__(message)
		self.status_code = status_code


@dataclasses.dataclass(frozen=True, slots=True)
class CentralAuthResult:
	ok: bool
	label: str | None = None
	error: str | None = None


class CentralClient:
	"""Talks to a single Central instance. Constructed from Central Settings."""

	def __init__(
		self,
		url: str,
		api_key: str,
		api_secret: str,
		webhook_secret: str | None = None,
		timeout: int = DEFAULT_TIMEOUT,
	):
		self.url = url.rstrip("/")
		self.api_key = api_key
		self.api_secret = api_secret
		self.webhook_secret = webhook_secret
		self.timeout = timeout

	def ping(self) -> CentralAuthResult:
		"""Credential check. Never raises — returns ok=False for the Test Connection
		toast. Plain token auth, out of scope for the webhook HMAC scheme."""
		try:
			body = self._request("GET", "ping")
		except CentralError as exception:
			return CentralAuthResult(ok=False, error=str(exception))
		return CentralAuthResult(ok=True, label=body.get("label"))

	def post_event(self, event: dict) -> dict:
		return self._request("POST", "event", json=event, sign=True)

	def _sign(self, headers: dict, json_payload: dict | None) -> bytes:
		"""Sign the exact bytes sent, adding the X-Atlas-* headers in place. Returns the
		body so the caller passes `data=`, not `json=` — requests' own serialization
		isn't guaranteed byte-identical to what was signed."""
		from atlas.atlas.core.placement import atlas_region

		body_bytes = json.dumps(json_payload or {}).encode()
		timestamp = frappe.utils.now()
		signature = hmac.new(
			self.webhook_secret.encode(), f"{timestamp}.".encode() + body_bytes, hashlib.sha256
		).hexdigest()
		headers["X-Atlas-Region"] = atlas_region()
		headers["X-Atlas-Timestamp"] = timestamp
		headers["X-Atlas-Signature"] = signature
		return body_bytes

	def _request(self, method: str, route_key: str, json: dict | None = None, sign: bool = False) -> dict:
		url = f"{self.url}/api/method/{_ROUTES[route_key]}"
		headers = {
			"Content-Type": "application/json",
			"Accept": "application/json",
		}
		data = None
		if sign:
			if not self.webhook_secret:
				raise CentralError(f"cannot sign {route_key}: no webhook_secret configured")
			data = self._sign(headers, json_payload=json)
			json = None
		else:
			headers["Authorization"] = f"token {self.api_key}:{self.api_secret}"
		try:
			response = requests.request(
				method, url, json=json, data=data, headers=headers, timeout=self.timeout
			)
		except requests.RequestException as exception:
			raise CentralError(f"{method} {route_key}: {exception}") from exception
		if response.status_code >= 400:
			raise CentralError(
				f"{method} {route_key} -> {response.status_code}: {response.text}", response.status_code
			)
		if not response.content:
			return {}
		body = response.json()
		# Frappe wraps whitelisted return values in {"message": ...}. Unwrap so
		# callers see Central's payload directly, but tolerate a bare object too.
		if isinstance(body, dict) and "message" in body:
			message = body["message"]
			return message if isinstance(message, dict) else {"message": message}
		return body
