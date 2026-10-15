from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
import urllib3

from atlas.atlas.core.server_providers.base import ProviderOperationError, ServerPowerAction

logger = logging.getLogger("atlas.provider.redfish")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

READ_TIMEOUT_SECONDS = 10
ACTION_TIMEOUT_SECONDS = 30
# Large lifecycle logs can exhaust the read budget.
MAXIMUM_MEMBERS = 1024
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
	"""Read and control a host through its Redfish ComputerSystem URL."""

	def __init__(self, url: str, username: str, password: str, deadline: float | None = None) -> None:
		self.url = url
		self.username = username
		self.password = password
		self.deadline = deadline

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
		allowed = self.read_allowed_reset_types(reset) or RESET_TYPES[action][:1]
		reset_type = next((value for value in RESET_TYPES[action] if value in allowed), None)
		if not reset_type:
			raise RedfishError(f"{self.url} allows none of {', '.join(RESET_TYPES[action])}")

		self.request("POST", urljoin(self.url, reset["target"]), json={"ResetType": reset_type})

	def read_allowed_reset_types(self, reset: dict) -> list[str]:
		"""Return the ResetType values from the action, or from its ActionInfo resource."""
		if reset.get("ResetType@Redfish.AllowableValues"):
			return reset["ResetType@Redfish.AllowableValues"]

		if not isinstance(reset.get("@Redfish.ActionInfo"), str):
			return []

		for parameter in self.read(reset["@Redfish.ActionInfo"]).get("Parameters") or []:
			if parameter.get("Name") == "ResetType":
				return parameter.get("AllowableValues") or []
		return []

	def read(self, link: str) -> dict[str, Any]:
		"""Read a Redfish resource by its @odata.id link."""
		return self.request("GET", urljoin(self.url, link))

	def read_members(self, link: str) -> list[dict[str, Any]]:
		"""Read all collection pages within the member limit."""
		page = self.read(link)
		count = page.get("Members@odata.count")
		self.check_member_limit(link, count or 0)
		members = page.get("Members") or []
		# Some BMCs return all members with a next-page link. Stop at the reported count.
		while page.get("Members@odata.nextLink") and (count is None or len(members) < count):
			page = self.read(page["Members@odata.nextLink"])
			if not page.get("Members"):
				break
			members += page["Members"]
			self.check_member_limit(link, len(members))
		return members

	@staticmethod
	def check_member_limit(link: str, size: int) -> None:
		"""Refuse a collection that costs too many reads."""
		if size > MAXIMUM_MEMBERS:
			raise RedfishError(f"{link} has more than {MAXIMUM_MEMBERS} members")

	def read_log_links(self, resource: dict) -> list[str]:
		"""List a resource's log service links."""
		link = self.get_link(resource.get("LogServices"))
		return [self.get_link(member) for member in self.read_members(link)] if link else []

	def read_log(self, link: str) -> tuple[str, list[dict[str, Any]]]:
		"""Read a log service's name and entries."""
		service = self.read(link)
		entries_link = self.get_link(service.get("Entries"))
		return self.get_name(service), self.read_members(entries_link) if entries_link else []

	def get_timeout(self, is_read: bool) -> float:
		"""Return the request timeout, cut to the time left before the deadline."""
		timeout = READ_TIMEOUT_SECONDS if is_read else ACTION_TIMEOUT_SECONDS
		if self.deadline is None:
			return timeout

		timeout = min(timeout, self.deadline - time.monotonic())
		if timeout <= 0:
			raise RedfishError(f"No request to {self.url} starts after the BMC time limit")
		return timeout

	def request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
		"""Return the JSON object of one Redfish request to the BMC host."""
		# A BMC response must not send the credentials to another host.
		if urlsplit(url)[:2] != urlsplit(self.url)[:2]:
			raise RedfishError(f"{url} is not on the BMC host of {self.url}")

		# The outcome of a failed action is unknown, and repeating it is not safe.
		is_read = method == "GET"
		timeout = self.get_timeout(is_read)

		try:
			response = requests.request(
				method,
				url,
				auth=(self.username, self.password),
				headers={"Accept": "application/json"},
				timeout=timeout,
				allow_redirects=False,
				verify=False,  # A BMC has a self-signed certificate; password auth protects the request.
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
	def get_link(value: dict | list | None) -> str | None:
		"""Return @odata.id from a link or the first link in a list."""
		if isinstance(value, list):
			value = value[0] if value else None
		return value.get("@odata.id") if isinstance(value, dict) else None

	@staticmethod
	def get_name(resource: dict) -> str:
		"""Return a resource's name, falling back to its member ID or ID."""
		return str(resource.get("Name") or resource.get("MemberId") or resource.get("Id") or "")

	@staticmethod
	def is_present(resource: dict) -> bool:
		"""Return whether Redfish marks the resource as present."""
		return (resource.get("Status") or {}).get("State") != "Absent"

	@staticmethod
	def _error_message(response: requests.Response) -> str:
		try:
			error = response.json().get("error")
		except ValueError, AttributeError:
			return ""
		message = error.get("message") if isinstance(error, dict) else None
		return f": {message[:200]}" if isinstance(message, str) else ""
