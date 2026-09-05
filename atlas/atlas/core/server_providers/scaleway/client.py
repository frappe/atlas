from __future__ import annotations

import logging
from typing import Any

import requests

from atlas.atlas.core.server_providers.base import ProviderOperationError

logger = logging.getLogger("atlas.provider.scaleway")


class ScalewayError(ProviderOperationError):
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
		logger.info(
			"Provider request started",
			extra={"provider": "Scaleway", "operation": method, "resource": path},
		)
		try:
			response = requests.request(
				method,
				f"{self.base_url}{path}",
				headers={"X-Auth-Token": self.secret_key},
				timeout=30,
				**kwargs,
			)
		except requests.RequestException as error:
			logger.warning(
				"Provider request failed",
				extra={"provider": "Scaleway", "operation": method, "resource": path},
			)
			raise ScalewayError(
				f"{method} {path} could not reach Scaleway",
				code="provider_transport_error",
				is_retryable=True,
			) from error
		if allow_missing and response.status_code == 404:
			return {}
		if response.status_code >= 400:
			logger.warning(
				"Provider request failed",
				extra={
					"provider": "Scaleway",
					"operation": method,
					"resource": path,
					"status": response.status_code,
				},
			)
			raise ScalewayError(
				f"{method} {path} failed with status {response.status_code}",
				code="provider_http_error",
				is_retryable=response.status_code == 429 or response.status_code >= 500,
			)
		if not response.content:
			return {}
		result = response.json()
		if not isinstance(result, dict):
			raise ScalewayError(f"{method} {path} returned a non-object JSON response")
		return result
