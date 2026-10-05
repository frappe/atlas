from __future__ import annotations

from time import monotonic, sleep
from typing import override
from urllib.parse import urljoin, urlsplit, urlunsplit

import frappe
import requests

from atlas.atlas.core.bmc_providers.base import BMCError, BMCPowerStatus, BMCProvider
from atlas.atlas.core.server_providers.base import ServerPowerAction

POWER_TIMEOUT_SECONDS = 120
POWER_POLL_SECONDS = 2


class RedfishTransientReadError(BMCError):
	"""Identify a temporary GET failure that power polling can retry."""


class RedfishBMCProvider(BMCProvider):
	"""Control one ComputerSystem through a Redfish service."""

	def __init__(self, url: str, username: str = "", password: str = "") -> None:
		super().__init__(self._normalize_url(url.strip()), username, password)
		self.origin = urlsplit(self.url)[:2]
		self.auth = (username, password) if username else None

	@override
	def read_power_status(self) -> BMCPowerStatus:
		"""Read the power status of the system."""
		return self._power_status(self._read_system())

	@override
	def set_power_state(self, action: ServerPowerAction) -> None:
		"""Power on, gracefully shut down, or gracefully restart the system."""
		if action == ServerPowerAction.START:
			self._reset(("On", "ForceOn"), "On")
		elif action == ServerPowerAction.STOP:
			self._reset(("GracefulShutdown",), "Off", wait_for_power=False)
		else:
			self._reset(("GracefulRestart",), "On", reboot=True)

	@staticmethod
	def _normalize_url(url: str) -> str:
		try:
			parts = urlsplit(url)
			port = parts.port
		except ValueError:
			raise BMCError("The Redfish URL is invalid") from None

		schemes = ("http", "https") if frappe.conf.developer_mode else ("https",)
		if (
			parts.scheme not in schemes
			or not parts.hostname
			or parts.username is not None
			or parts.password is not None
			or parts.query
			or parts.fragment
		):
			raise BMCError("Use an HTTPS Redfish URL without embedded credentials, query, or fragment")

		path = parts.path.rstrip("/") or "/redfish/v1"
		if path != "/redfish/v1" and not path.startswith("/redfish/v1/"):
			raise BMCError("The Redfish URL must be under /redfish/v1")

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
			raise BMCError(f"The Redfish response has no {field} link")

		url = self._normalize_url(urljoin(self.url, link))
		if urlsplit(url)[:2] != self.origin:
			raise BMCError("Redfish links must stay on the configured service")
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
		except requests.exceptions.SSLError:
			raise BMCError("Could not establish a verified Redfish TLS connection") from None
		except requests.Timeout, requests.ConnectionError:
			raise RedfishTransientReadError("Could not connect to the Redfish service") from None
		except requests.RequestException:
			raise BMCError("Could not connect to the Redfish service") from None

		if response.status_code in (500, 502, 503, 504):
			raise RedfishTransientReadError(f"Redfish returned HTTP {response.status_code}")
		if response.status_code != 200:
			raise BMCError(f"Redfish returned HTTP {response.status_code}")
		try:
			resource = response.json()
		except ValueError:
			raise BMCError("Redfish returned invalid JSON") from None
		if not isinstance(resource, dict):
			raise BMCError("Redfish must return a JSON object")
		return resource

	def _reset(
		self,
		reset_types: tuple[str, ...],
		expected_state: str,
		*,
		reboot: bool = False,
		wait_for_power: bool = True,
	) -> None:
		resource = self._read_system()
		current = self._power_status(resource).power_state
		if current == expected_state and not reboot:
			return
		if current not in ("On", "Off") or (reboot and current != "On"):
			raise BMCError("Power actions require a stable On or Off state; reboot requires On")

		actions = resource.get("Actions")
		action = actions.get("#ComputerSystem.Reset") if isinstance(actions, dict) else None
		if not isinstance(action, dict):
			raise BMCError("The Redfish system has no Reset action")
		target = self._resolve_link(action, "target")
		allowed = self._reset_types(action)
		reset_type = next((value for value in reset_types if value in allowed), None)
		if reset_type is None:
			raise BMCError(f"The Redfish system does not advertise {' or '.join(reset_types)}")

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
			raise BMCError(
				"The Redfish reset outcome is unknown; reload the server before another action"
			) from None
		if response.status_code not in (200, 202, 204):
			raise BMCError(f"Redfish reset returned HTTP {response.status_code}")
		if not wait_for_power:
			return

		if response.status_code == 202:
			self._wait_for_task(response, deadline)
		last_read_error: RedfishTransientReadError | None = None
		while monotonic() < deadline:
			try:
				status = self.read_power_status()
			except RedfishTransientReadError as error:
				last_read_error = error
			else:
				if status.power_state == expected_state:
					return
				last_read_error = None
			sleep(min(POWER_POLL_SECONDS, max(0, deadline - monotonic())))
		detail = f"; last power read failed: {last_read_error}" if last_read_error else ""
		raise BMCError(
			f"Redfish did not report {expected_state} within {POWER_TIMEOUT_SECONDS} seconds{detail}"
		)

	def _reset_types(self, action: dict) -> tuple[str, ...]:
		values = action.get("ResetType@Redfish.AllowableValues")
		if values is None and action.get("@Redfish.ActionInfo"):
			information = self._read(self._resolve_link(action, "@Redfish.ActionInfo"))
			parameters = information.get("Parameters")
			if not isinstance(parameters, list):
				raise BMCError("Redfish ActionInfo has no parameter list")
			values = next(
				(
					parameter.get("AllowableValues")
					for parameter in parameters
					if isinstance(parameter, dict) and parameter.get("Name") == "ResetType"
				),
				None,
			)
		if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
			raise BMCError("Redfish must advertise the allowable ResetType values")
		return tuple(values)

	def _wait_for_task(self, response: requests.Response, deadline: float) -> None:
		monitor = self._resolve_link({"Location": response.headers.get("Location")}, "Location")
		while monotonic() < deadline:
			if response.status_code not in (200, 202, 204):
				raise BMCError(f"Redfish reset task returned HTTP {response.status_code}")
			task = {}
			if response.content and response.status_code != 204:
				try:
					task = response.json()
				except ValueError:
					raise BMCError("Redfish reset task returned invalid JSON") from None
				if (
					not isinstance(task, dict)
					or "error" in task
					or task.get("TaskState") in ("Exception", "Killed", "Cancelled", "Interrupted")
					or task.get("TaskStatus") == "Critical"
				):
					raise BMCError("The Redfish reset task failed")
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
				raise BMCError("Could not read the Redfish reset task") from None
		raise BMCError("The Redfish reset task did not finish within the power timeout")

	def _read_system(self) -> dict:
		resource = self._read(self.url)
		resource_type = resource.get("@odata.type")
		if not isinstance(resource_type, str) or not resource_type.startswith("#ComputerSystem."):
			raise BMCError("The Redfish URL does not identify a ComputerSystem")
		if self._resolve_link(resource, "@odata.id") != self.url:
			raise BMCError("Redfish returned a different system from the configured URL")
		return resource

	@staticmethod
	def _power_status(resource: dict) -> BMCPowerStatus:
		power_state = resource.get("PowerState")
		if power_state not in ("On", "Off", "PoweringOn", "PoweringOff", "Paused", "Sleeping", "Hibernating"):
			raise BMCError("Redfish returned an unsupported or missing PowerState")
		status = resource.get("Status", {})
		if not isinstance(status, dict):
			raise BMCError("Redfish returned invalid system Status")
		health = status.get("Health")
		if health is not None and not isinstance(health, str):
			raise BMCError("Redfish returned invalid system health")
		return BMCPowerStatus(power_state, health)
