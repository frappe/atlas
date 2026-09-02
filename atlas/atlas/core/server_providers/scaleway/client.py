from __future__ import annotations

from typing import Any

import requests

from atlas.atlas.core.server_providers.base import ServerProviderError


class ScalewayError(ServerProviderError):
	"""Raised when Scaleway rejects a provider request."""


class ScalewayClient:
	"""Send authenticated requests to the Scaleway API."""

	base_url = "https://api.scaleway.com"

	def __init__(self, secret_key: str) -> None:
		self.secret_key = secret_key

	def request(
		self, method: str, path: str, *, allow_missing: bool = False, **kwargs: object
	) -> dict[str, Any]:
		"""Return the JSON response for one Scaleway API request."""
		response = requests.request(
			method,
			f"{self.base_url}{path}",
			headers={"X-Auth-Token": self.secret_key},
			timeout=30,
			**kwargs,
		)
		if allow_missing and response.status_code == 404:
			return {}
		if response.status_code >= 400:
			raise ScalewayError(f"{method} {path} failed with status {response.status_code}: {response.text}")
		if not response.content:
			return {}
		result = response.json()
		if not isinstance(result, dict):
			raise ScalewayError(f"{method} {path} returned a non-object JSON response")
		return result
