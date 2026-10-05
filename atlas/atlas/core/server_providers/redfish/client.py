from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from atlas.atlas.core.server_providers.base import ProviderOperationError


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
