from __future__ import annotations

from dataclasses import dataclass
from time import monotonic, sleep
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from atlas.atlas.core.server_providers.base import ProviderOperationError

POWER_TIMEOUT_SECONDS = 120
POWER_POLL_SECONDS = 2


class RedfishError(ProviderOperationError):
	"""Report a Redfish provider failure."""


@dataclass(frozen=True, slots=True)
class RedfishSystem:
	"""Identify one existing ComputerSystem."""

	url: str
	id: str
	name: str
	uuid: str | None


@dataclass(frozen=True, slots=True)
class RedfishPowerStatus:
	"""Store observed BMC power and health independently of Metal readiness."""

	power_state: str
	health: str | None


class RedfishClient:
	"""Read system identities from one Redfish service."""

	def __init__(self, url: str, username: str = "", password: str = "") -> None:
		self.url = self._normalize_url(url.strip())
		self.origin = urlsplit(self.url)[:2]
		if bool(username) != bool(password):
			raise RedfishError("Provide both the Redfish username and password, or leave both empty")
		self.auth = (username, password) if username else None

	@staticmethod
	def _normalize_url(url: str) -> str:
		try:
			parts = urlsplit(url)
			port = parts.port
		except ValueError:
			raise RedfishError("The Redfish URL is invalid") from None

		if (
			parts.scheme not in ("http", "https")
			or not parts.hostname
			or parts.username is not None
			or parts.password is not None
			or parts.query
			or parts.fragment
		):
			raise RedfishError(
				"Use an HTTP or HTTPS Redfish URL without embedded credentials, query, or fragment"
			)

		path = parts.path.rstrip("/") or "/redfish/v1"
		if path != "/redfish/v1" and not path.startswith("/redfish/v1/"):
			raise RedfishError("The Redfish URL must be under /redfish/v1")

		host = parts.hostname.lower()
		if ":" in host:
			host = f"[{host}]"
		if port is not None and (parts.scheme, port) not in (("http", 80), ("https", 443)):
			host = f"{host}:{port}"
		return urlunsplit((parts.scheme, host, path, "", ""))

	def _resolve_link(self, resource: dict, field: str) -> str:
		value = resource.get(field)
		link = value.get("@odata.id") if isinstance(value, dict) else value
		if not isinstance(link, str) or not link:
			raise RedfishError(f"The Redfish response has no {field} link")

		url = self._normalize_url(urljoin(self.url, link))
		if urlsplit(url)[:2] != self.origin:
			raise RedfishError("Redfish links must stay on the configured service")
		return url

	def _read(self, url: str) -> dict:
		try:
			response = requests.get(
				url,
				auth=self.auth,
				headers={"Accept": "application/json"},
				timeout=10,
				allow_redirects=False,
			)
		except requests.RequestException:
			raise RedfishError("Could not connect to the Redfish service") from None

		if response.status_code != 200:
			raise RedfishError(f"Redfish returned HTTP {response.status_code}")
		try:
			resource = response.json()
		except ValueError:
			raise RedfishError("Redfish returned invalid JSON") from None
		if not isinstance(resource, dict):
			raise RedfishError("Redfish must return a JSON object")
		return resource

	def discover_system(self) -> RedfishSystem:
		"""Follow the advertised links to exactly one ComputerSystem."""
		resource = self._read(self.url)
		resource_type = resource.get("@odata.type", "")
		if isinstance(resource_type, str) and resource_type.startswith("#ServiceRoot."):
			resource = self._read(self._resolve_link(resource, "Systems"))

		resource_type = resource.get("@odata.type", "")
		if isinstance(resource_type, str) and resource_type.startswith("#ComputerSystemCollection."):
			members = resource.get("Members")
			if not isinstance(members, list) or len(members) != 1 or not isinstance(members[0], dict):
				raise RedfishError(
					"Use a specific system URL when the Systems collection does not contain one machine"
				)
			resource = self._read(self._resolve_link(members[0], "@odata.id"))

		resource_type = resource.get("@odata.type", "")
		if not isinstance(resource_type, str) or not resource_type.startswith("#ComputerSystem."):
			raise RedfishError("The Redfish URL does not identify a ComputerSystem")
		system_id = resource.get("Id")
		name = resource.get("Name")
		uuid = resource.get("UUID")
		if not isinstance(system_id, str) or not system_id or not isinstance(name, str) or not name:
			raise RedfishError("The Redfish system must have an Id and Name")
		if uuid is not None and not isinstance(uuid, str):
			raise RedfishError("The Redfish system UUID must be a string")
		return RedfishSystem(self._resolve_link(resource, "@odata.id"), system_id, name, uuid)

	def read_power_status(self) -> RedfishPowerStatus:
		"""Read the power status of the registered system."""
		return self._power_status(self._read_system())

	def power_on(self) -> None:
		"""Request an advertised power-on action and observe On."""
		self._reset(("On", "ForceOn"), "On")

	def _reset(self, reset_types: tuple[str, ...], expected_state: str, *, reboot: bool = False) -> None:
		resource = self._read_system()
		current = self._power_status(resource).power_state
		if current == expected_state and not reboot:
			return
		if current not in ("On", "Off") or (reboot and current != "On"):
			raise RedfishError("Power actions require a stable On or Off state; reboot requires On")
		actions = resource.get("Actions")
		action = actions.get("#ComputerSystem.Reset") if isinstance(actions, dict) else None
		if not isinstance(action, dict):
			raise RedfishError("The Redfish system has no Reset action")
		target = self._resolve_link(action, "target")
		allowed = self._reset_types(action)
		reset_type = next((value for value in reset_types if value in allowed), None)
		if reset_type is None:
			raise RedfishError(f"The Redfish system does not advertise {' or '.join(reset_types)}")

		deadline = monotonic() + POWER_TIMEOUT_SECONDS
		try:
			response = requests.post(
				target,
				auth=self.auth,
				headers={"Accept": "application/json"},
				json={"ResetType": reset_type},
				timeout=10,
				allow_redirects=False,
			)
		except requests.RequestException:
			raise RedfishError(
				"The Redfish reset outcome is unknown; refresh power state before another action"
			) from None
		if response.status_code not in (200, 202, 204):
			raise RedfishError(f"Redfish reset returned HTTP {response.status_code}")
		if response.status_code == 202:
			self._wait_for_task(response, deadline)
		while monotonic() < deadline:
			if self.read_power_status().power_state == expected_state:
				return
			sleep(min(POWER_POLL_SECONDS, max(0, deadline - monotonic())))
		raise RedfishError(
			f"Redfish did not report {expected_state} within {POWER_TIMEOUT_SECONDS} seconds; refresh power state"
		)

	def _reset_types(self, action: dict) -> tuple[str, ...]:
		values = action.get("ResetType@Redfish.AllowableValues")
		if values is None and action.get("@Redfish.ActionInfo"):
			information = self._read(self._resolve_link(action, "@Redfish.ActionInfo"))
			parameters = information.get("Parameters")
			if not isinstance(parameters, list):
				raise RedfishError("Redfish ActionInfo has no parameter list")
			values = next(
				(
					parameter.get("AllowableValues")
					for parameter in parameters
					if isinstance(parameter, dict) and parameter.get("Name") == "ResetType"
				),
				None,
			)
		if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
			raise RedfishError("Redfish must advertise the allowable ResetType values")
		return tuple(values)

	def _wait_for_task(self, response: requests.Response, deadline: float) -> None:
		monitor = self._resolve_link({"Location": response.headers.get("Location")}, "Location")
		while monotonic() < deadline:
			if response.status_code not in (200, 202, 204):
				raise RedfishError(f"Redfish reset task returned HTTP {response.status_code}")
			task = {}
			if response.content and response.status_code != 204:
				try:
					task = response.json()
				except ValueError:
					raise RedfishError("Redfish reset task returned invalid JSON") from None
				if (
					not isinstance(task, dict)
					or "error" in task
					or task.get("TaskState") in ("Exception", "Killed", "Cancelled", "Interrupted")
					or task.get("TaskStatus") == "Critical"
				):
					raise RedfishError("The Redfish reset task failed")
			if response.status_code == 204 or (
				response.status_code == 200 and task.get("TaskState") in (None, "Completed")
			):
				return
			try:
				delay = float(response.headers.get("Retry-After", POWER_POLL_SECONDS))
			except TypeError, ValueError:
				delay = POWER_POLL_SECONDS
			sleep(min(max(POWER_POLL_SECONDS, min(delay, 30)), max(0, deadline - monotonic())))
			if monotonic() >= deadline:
				break
			try:
				response = requests.get(
					monitor,
					auth=self.auth,
					headers={"Accept": "application/json"},
					timeout=10,
					allow_redirects=False,
				)
			except requests.RequestException:
				raise RedfishError("Could not read the Redfish reset task; refresh power state") from None
		raise RedfishError(
			"The Redfish reset task did not finish within the power timeout; refresh power state"
		)

	def _read_system(self) -> dict:
		resource = self._read(self.url)
		resource_type = resource.get("@odata.type", "")
		if not isinstance(resource_type, str) or not resource_type.startswith("#ComputerSystem."):
			raise RedfishError("Use the registered ComputerSystem URL for power operations")
		if self._resolve_link(resource, "@odata.id") != self.url:
			raise RedfishError("Redfish returned a different system from the registered URL")
		return resource

	@staticmethod
	def _power_status(resource: dict) -> RedfishPowerStatus:
		power_state = resource.get("PowerState")
		if power_state not in ("On", "Off", "PoweringOn", "PoweringOff", "Paused", "Sleeping", "Hibernating"):
			raise RedfishError("Redfish returned an unsupported or missing PowerState")
		status = resource.get("Status", {})
		if not isinstance(status, dict):
			raise RedfishError("Redfish returned invalid system Status")
		health = status.get("Health")
		if health is not None and not isinstance(health, str):
			raise RedfishError("Redfish returned invalid system health")
		return RedfishPowerStatus(power_state, health)
