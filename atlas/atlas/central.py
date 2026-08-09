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
  check for the Test Connection toast.
- **event** — `central.api.atlas.event` (via `post_event`) carries VM lifecycle
  events, authenticated as the pushed per-Atlas service user. Atlas's outbound is
  unrestricted, so this works regardless of the management-plane firewall.

The route names and payloads are the single external dependency; the whole
contract is absorbed here, so a change on Central's side is a one-file edit.
"""

from __future__ import annotations

import dataclasses

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

	def __init__(self, url: str, api_key: str, api_secret: str, timeout: int = DEFAULT_TIMEOUT):
		self.url = url.rstrip("/")
		self.api_key = api_key
		self.api_secret = api_secret
		self.timeout = timeout

	def ping(self) -> CentralAuthResult:
		"""Credential check. Never raises — returns ok=False so the Test
		Connection toast can render a red indicator."""
		try:
			body = self._request("GET", "ping")
		except CentralError as exception:
			return CentralAuthResult(ok=False, error=str(exception))
		return CentralAuthResult(ok=True, label=body.get("label"))

	def post_event(self, event: dict) -> dict:
		return self._request("POST", "event", json=event)

	def _request(self, method: str, route_key: str, json: dict | None = None) -> dict:
		url = f"{self.url}/api/method/{_ROUTES[route_key]}"
		headers = {
			"Authorization": f"token {self.api_key}:{self.api_secret}",
			"Content-Type": "application/json",
			"Accept": "application/json",
		}
		try:
			response = requests.request(method, url, json=json, headers=headers, timeout=self.timeout)
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
