from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import frappe
import requests

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

ADMIN_API_PATH = "/@warpgate/admin/api"
PROFILE_API_PATH = "/@warpgate/api/profile"
REQUEST_TIMEOUT_SECONDS = 15
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class WarpgateError(Exception):
	"""A Warpgate admin API call failed."""


class WarpgateClient:
	"""Call the Warpgate admin API of this region as the atlas user."""

	def __init__(self, url: str, token: str) -> None:
		self.url = url.rstrip("/")
		self.session = requests.Session()
		self.session.headers["X-Warpgate-Token"] = token
		# Warpgate serves its own certificate on loopback. Public names use the region certificate.
		self.session.verify = urlsplit(url).hostname not in LOOPBACK_HOSTS

	@classmethod
	def from_settings(cls, settings: AtlasSettings | None = None) -> WarpgateClient | None:
		"""Return a client, or None when this region has no Warpgate."""
		settings = settings or frappe.get_single("Atlas Settings")
		token = settings.get_password("warpgate_api_token", raise_exception=False)
		if not settings.warpgate_url or not token:
			return None
		return cls(settings.warpgate_url, token)

	def list_targets(self) -> list[dict]:
		return self._request("GET", "/targets")

	def create_target(self, data: dict) -> dict:
		return self._request("POST", "/targets", json=data)

	def update_target(self, target_id: str, data: dict) -> dict:
		return self._request("PUT", f"/targets/{target_id}", json=data)

	def delete_target(self, target_id: str) -> None:
		self._request("DELETE", f"/targets/{target_id}")

	def list_target_roles(self, target_id: str) -> list[dict]:
		return self._request("GET", f"/targets/{target_id}/roles")

	def add_target_role(self, target_id: str, role_id: str) -> None:
		self._request("POST", f"/targets/{target_id}/roles/{role_id}")

	def list_roles(self) -> list[dict]:
		return self._request("GET", "/roles")

	def create_role(self, name: str) -> dict:
		return self._request("POST", "/roles", json={"name": name})

	def rename_role(self, role_id: str, name: str) -> None:
		self._request("PUT", f"/role/{role_id}", json={"name": name})

	def delete_role(self, role_id: str) -> None:
		self._request("DELETE", f"/role/{role_id}")

	def find_user(self, username: str) -> dict | None:
		"""Return the user with this username, compared without case like Warpgate does."""
		users = self._request("GET", "/users", params={"search": username})
		return next((user for user in users if user["username"].lower() == username.lower()), None)

	def create_user(self, username: str) -> dict:
		return self._request("POST", "/users", json={"username": username})

	def get_user(self, user_id: str) -> dict:
		return self._request("GET", f"/users/{user_id}")

	def set_credential_policy(self, user: dict, credential_policy: dict) -> None:
		self._request(
			"PUT",
			f"/users/{user['id']}",
			json={"username": user["username"], "credential_policy": credential_policy},
		)

	def list_sso_credentials(self, user_id: str) -> list[dict]:
		return self._request("GET", f"/users/{user_id}/credentials/sso")

	def add_sso_credential(self, user_id: str, provider: str, email: str) -> None:
		self._request(
			"POST", f"/users/{user_id}/credentials/sso", json={"provider": provider, "email": email}
		)

	def list_user_roles(self, user_id: str) -> list[dict]:
		return self._request("GET", f"/users/{user_id}/roles")

	def grant_user_role(self, user_id: str, role_id: str, expires_at: datetime, *, is_granted: bool) -> None:
		"""Grant a role until expires_at, or move the end of an existing grant."""
		method = "PUT" if is_granted else "POST"
		self._request(
			method, f"/users/{user_id}/roles/{role_id}", json={"expires_at": expires_at.isoformat()}
		)

	def revoke_user_role(self, user_id: str, role_id: str) -> None:
		self._request("DELETE", f"/users/{user_id}/roles/{role_id}")

	def list_known_hosts(self) -> list[dict]:
		return self._request("GET", "/ssh/known-hosts")

	def add_known_host(self, host: str, key_type: str, key_base64: str) -> None:
		self._request(
			"POST",
			"/ssh/known-hosts",
			json={"host": host, "port": 22, "key_type": key_type, "key_base64": key_base64},
		)

	def get_public_keys(self) -> list[str]:
		"""Return the default client keys that every host must trust for root."""
		return [key["public_key"] for key in self._request("GET", "/ssh/own-keys") if key["is_default"]]

	def list_api_tokens(self) -> list[dict]:
		return self._request("GET", "/api-tokens", api_path=PROFILE_API_PATH)

	def create_api_token(self, expires_at: datetime) -> dict:
		"""Return a new token of the atlas user. The response holds its secret once."""
		return self._request(
			"POST",
			"/api-tokens",
			api_path=PROFILE_API_PATH,
			json={"label": "atlas", "expiry": expires_at.isoformat()},
		)

	def delete_api_token(self, token_id: str) -> None:
		self._request("DELETE", f"/api-tokens/{token_id}", api_path=PROFILE_API_PATH)

	def _request(self, method: str, path: str, *, api_path: str = ADMIN_API_PATH, **arguments: Any) -> Any:
		try:
			response = self.session.request(
				method, self.url + api_path + path, timeout=REQUEST_TIMEOUT_SECONDS, **arguments
			)
		except requests.RequestException as error:
			raise WarpgateError(f"Warpgate is unreachable: {error}") from error
		if response.status_code >= 400:
			raise WarpgateError(
				f"Warpgate {method} {path} returned {response.status_code}: {response.text[:300]}"
			)
		return response.json() if response.content else None
