from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from atlas.atlas.core.server_providers.base import ProviderOperationError, ServerPowerAction

logger = logging.getLogger("atlas.provider.redfish")

READ_TIMEOUT_SECONDS = 10
ACTION_TIMEOUT_SECONDS = 30
# Atlas sends the first ResetType that the BMC allows. A forced type is only a fallback for a BMC
# that does not offer the graceful one, never a retry.
RESET_TYPES = {
	ServerPowerAction.START: ("On", "ForceOn"),
	ServerPowerAction.STOP: ("GracefulShutdown", "ForceOff"),
	ServerPowerAction.REBOOT: ("GracefulRestart", "ForceRestart"),
}


class RedfishError(ProviderOperationError):
	"""Report a failed Redfish request."""


class RedfishClient:
	"""Control one Generic host through the ComputerSystem resource of its BMC Redfish API."""

	def __init__(self, url: str, username: str, password: str) -> None:
		self.url = url
		self.username = username
		self.password = password

	def read_power_state(self) -> str:
		"""Return the Redfish PowerState of the system, such as On or Off."""
		power_state = self.request("GET", self.url).get("PowerState")
		if not isinstance(power_state, str):
			raise RedfishError(f"{self.url} returned no PowerState. Use the ComputerSystem URL.")
		return power_state

	def set_power_state(self, action: ServerPowerAction) -> None:
		"""Submit the Reset action for one power action. Do not wait for the new power state."""
		actions = self.request("GET", self.url).get("Actions")
		reset = actions.get("#ComputerSystem.Reset") if isinstance(actions, dict) else None
		if not isinstance(reset, dict) or not isinstance(reset.get("target"), str):
			raise RedfishError(f"{self.url} has no ComputerSystem.Reset action")

		# Redfish makes the allowed values optional. Without them, Atlas sends its first choice.
		allowed = reset.get("ResetType@Redfish.AllowableValues") or RESET_TYPES[action][:1]
		reset_type = next((value for value in RESET_TYPES[action] if value in allowed), None)
		if not reset_type:
			raise RedfishError(f"{self.url} allows none of {', '.join(RESET_TYPES[action])}")

		self.request("POST", urljoin(self.url, reset["target"]), json={"ResetType": reset_type})

	def request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
		"""Return the JSON object of one Redfish request to the BMC host."""
		# A BMC response must not send the credentials to another host.
		if urlsplit(url)[:2] != urlsplit(self.url)[:2]:
			raise RedfishError(f"{url} is not on the BMC host of {self.url}")

		# The outcome of a failed action is unknown, and repeating it is not safe.
		is_read = method == "GET"
		try:
			response = requests.request(
				method,
				url,
				auth=(self.username, self.password),
				headers={"Accept": "application/json"},
				timeout=READ_TIMEOUT_SECONDS if is_read else ACTION_TIMEOUT_SECONDS,
				allow_redirects=False,
				**kwargs,
			)
		except requests.RequestException as error:
			logger.warning("Redfish request failed", extra={"operation": method, "resource": url})
			raise RedfishError(
				f"{method} {url} could not reach the BMC",
				code="provider_transport_error",
				is_retryable=is_read,
			) from error

		if response.status_code >= 300:
			logger.warning(
				"Redfish request failed",
				extra={"operation": method, "resource": url, "status": response.status_code},
			)
			raise RedfishError(
				f"{method} {url} failed with status {response.status_code}{self._error_message(response)}",
				code="provider_http_error",
				is_retryable=is_read and (response.status_code == 429 or response.status_code >= 500),
			)
		if not response.content:
			return {}

		try:
			result = response.json()
		except ValueError:
			raise RedfishError(f"{method} {url} returned a response that is not JSON") from None
		if not isinstance(result, dict):
			raise RedfishError(f"{method} {url} returned a non-object JSON response")
		return result

	@staticmethod
	def _error_message(response: requests.Response) -> str:
		try:
			error = response.json().get("error")
		except (ValueError, AttributeError):
			return ""
		message = error.get("message") if isinstance(error, dict) else None
		return f": {message[:200]}" if isinstance(message, str) else ""
