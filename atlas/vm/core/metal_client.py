from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import frappe
import requests
from frappe import _
from frappe.utils.password import get_decrypted_password

if TYPE_CHECKING:
	from atlas.server.doctype.server.server import Server


class MetalClientError(Exception):
	"""Store details for one failed Metal request."""

	def __init__(
		self,
		message: str,
		*,
		status: int | None = None,
		code: str | None = None,
		retryable: bool = False,
		uncertain: bool = False,
	) -> None:
		super().__init__(message)
		self.status = status
		self.code = code
		self.retryable = retryable
		self.uncertain = uncertain

	@property
	def is_not_found(self) -> bool:
		return self.status == 404


class MetalClient:
	"""Call the Metal API on one bare-metal Server."""

	timeout_seconds = (3, 10)
	create_timeout_seconds = (3, 15)
	status_timeout_seconds = (5, 30)
	snapshot_timeout_seconds = (5, 3600)

	def __init__(self, server: "Server") -> None:
		if not server.private_ipv4_address:
			raise MetalClientError(f"Server {server.name} has no private IPv4 address")
		token = get_decrypted_password("Server", server.name, "metald_api_token", raise_exception=False)
		if not token:
			raise MetalClientError(f"Server {server.name} has no Metal API token")

		self.base_url = f"http://{server.public_ipv4_address}:9000"
		self.headers = {"Authorization": f"Bearer {token}"}

	def get_console_connection(self, virtual_machine_id: str, mode: str = "tty") -> dict[str, str]:
		"""Return the websocket URL and auth header for a VM console."""
		websocket_url = self.base_url.replace("http://", "ws://", 1)
		return {
			"url": f"{websocket_url}/vms/{quote(virtual_machine_id, safe='')}/console?mode={mode}",
			"authorization": self.headers["Authorization"],
		}

	def put_virtual_machine(self, virtual_machine_id: str, request: dict[str, Any]) -> None:
		"""Store one VM request under its stable Atlas ID."""
		self._request(
			"PUT",
			f"/vms/{virtual_machine_id}",
			json=request,
			expected_status=202,
			uncertain_on_failure=True,
			timeout=self.create_timeout_seconds,
		)

	def get_virtual_machine(self, virtual_machine_id: str) -> dict[str, Any]:
		"""Return live data for one VM."""
		return self._request("GET", f"/vms/{virtual_machine_id}", timeout=self.status_timeout_seconds)

	def reboot_virtual_machine(self, virtual_machine_id: str) -> None:
		"""Request a background VM restart."""
		self._request("POST", f"/vms/{virtual_machine_id}/actions/reboot", expected_status=202)

	def terminate_virtual_machine(self, virtual_machine_id: str) -> None:
		"""Ask Metal to remove one VM."""
		self._request("POST", f"/vms/{virtual_machine_id}/actions/terminate", expected_status=202)

	def perform_action(self, virtual_machine_id: str, action: str) -> None:
		"""Ask Metal to apply one VM action."""
		self._request("POST", f"/vms/{virtual_machine_id}/actions/{action}", expected_status=202)

	def replace_virtual_machine_ssh_keys(
		self, virtual_machine_id: str, ssh_keys: list[str]
	) -> dict[str, Any]:
		"""Replace all authorized SSH keys for one VM."""
		return self._request(
			"PUT",
			f"/vms/{quote(virtual_machine_id, safe='')}/ssh-keys",
			json={"ssh_keys": ssh_keys},
			expected_status=200,
		)

	def resize_virtual_machine_disk(self, virtual_machine_id: str, disk_mib: int) -> None:
		"""Ask Metal to increase one VM disk size."""
		self._request(
			"POST",
			f"/vms/{virtual_machine_id}/resize/disk",
			json={"disk_mib": disk_mib},
			expected_status=202,
		)

	def resize_virtual_machine_compute(self, virtual_machine_id: str, vcpus: int, memory_mib: int) -> None:
		"""Ask Metal to change VM CPU and memory. The VM must be stopped."""
		self._request(
			"POST",
			f"/vms/{virtual_machine_id}/resize/compute",
			json={"vcpus": vcpus, "memory_mib": memory_mib},
			expected_status=202,
		)

	def create_snapshot(self, virtual_machine_id: str) -> dict[str, Any]:
		"""Create local image staging for one VM."""
		return self._request(
			"POST",
			f"/vms/{quote(virtual_machine_id, safe='')}/snapshots",
			expected_status=201,
			uncertain_on_failure=True,
			timeout=self.snapshot_timeout_seconds,
		)

	def start_snapshot_upload(self, snapshot_id: str, request: dict[str, Any]) -> None:
		"""Ask Metal to start uploading staged artifacts. Returns at once."""
		self._request(
			"POST",
			f"/snapshots/{quote(snapshot_id, safe='')}/upload",
			json=request,
			expected_status=202,
			uncertain_on_failure=True,
			timeout=self.snapshot_timeout_seconds,
		)

	def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
		"""Get the upload status for one staged snapshot."""
		return self._request(
			"GET",
			f"/snapshots/{quote(snapshot_id, safe='')}",
			timeout=self.snapshot_timeout_seconds,
		)

	def delete_snapshot(self, snapshot_id: str) -> None:
		"""Delete local image staging."""
		self._request(
			"DELETE",
			f"/snapshots/{quote(snapshot_id, safe='')}",
			uncertain_on_failure=True,
			timeout=self.snapshot_timeout_seconds,
		)

	def sync(self, wireguard_peers: list[dict[str, Any]], images: list[dict[str, Any]]) -> dict[str, Any]:
		"""Exchange controller and host state."""
		return self._request(
			"POST",
			"/sync",
			json={"wireguard_peers": wireguard_peers, "images": images},
		)

	def _request(
		self,
		method: str,
		path: str,
		*,
		expected_status: int | None = None,
		uncertain_on_failure: bool = False,
		**kwargs: Any,
	) -> dict[str, Any]:
		timeout = kwargs.pop("timeout", self.timeout_seconds)
		try:
			response = requests.request(
				method,
				f"{self.base_url}{path}",
				headers=self.headers,
				timeout=timeout,
				**kwargs,
			)
		except requests.RequestException as error:
			raise MetalClientError(str(error), retryable=True, uncertain=uncertain_on_failure) from error

		if response.status_code >= 400:
			message, code = self._error_data(response)
			retryable = response.status_code in {408, 429} or response.status_code >= 500
			raise MetalClientError(
				message,
				status=response.status_code,
				code=code,
				retryable=retryable,
				uncertain=uncertain_on_failure and retryable,
			)
		if expected_status is not None and response.status_code != expected_status:
			raise MetalClientError(
				f"Metal returned HTTP {response.status_code}, expected {expected_status}",
				status=response.status_code,
				uncertain=uncertain_on_failure,
			)
		if not response.content:
			return {}
		try:
			body = response.json()
		except ValueError as error:
			raise MetalClientError(
				"Metal returned invalid JSON",
				status=response.status_code,
				uncertain=uncertain_on_failure,
			) from error
		if not isinstance(body, dict):
			raise MetalClientError(
				"Metal returned a non-object JSON response",
				status=response.status_code,
				uncertain=uncertain_on_failure,
			)
		return body

	@staticmethod
	def _error_data(response: requests.Response) -> tuple[str, str | None]:
		try:
			body = response.json()
		except ValueError:
			return f"Metal returned HTTP {response.status_code}", None
		if isinstance(body, dict):
			error = body.get("error")
			if isinstance(error, dict):
				message = error.get("message")
				code = error.get("code")
				if isinstance(message, str):
					return message, code if isinstance(code, str) else None
		return f"Metal returned HTTP {response.status_code}", None


def throw_metal_error(error: MetalClientError) -> None:
	"""Show a Metal request error to the current user."""
	frappe.throw(_("Metal request failed: {0}").format(error))
