from __future__ import annotations

import ipaddress
import time
from time import monotonic
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import requests
from frappe.utils.password import get_decrypted_password

from atlas.vm.core.metal_models import MetalVirtualMachine

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


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
		"""Report whether Metal answered that the resource does not exist."""
		return self.status == 404


class MetalClient:
	"""Call the Metal API on one bare-metal Server."""

	api_port = 9000
	timeout_seconds = (5, 60)
	create_timeout_seconds = (5, 60)
	status_timeout_seconds = (5, 30)
	status_attempts = 3
	status_budget_seconds = 30
	retry_delay_seconds = 2
	snapshot_timeout_seconds = (5, 3600)

	def __init__(self, server: "MetalServer") -> None:
		self.base_url = self.get_api_url(server)
		token = get_decrypted_password("Metal Server", server.name, "metald_api_token", raise_exception=False)
		if not token:
			raise MetalClientError(f"Server {server.name} has no Metal API token")

		self.headers = {"Authorization": f"Bearer {token}"}

	@classmethod
	def get_api_url(cls, server: "MetalServer") -> str:
		"""Return the Metal HTTP address for one Server."""
		if not server.public_ipv4_address:
			raise MetalClientError(f"Server {server.name} has no public IPv4 address")
		try:
			public_ipv4_address = ipaddress.IPv4Address(server.public_ipv4_address)
		except (ipaddress.AddressValueError, TypeError) as error:
			raise MetalClientError(f"Server {server.name} has an invalid public IPv4 address") from error
		return f"http://{public_ipv4_address}:{cls.api_port}"

	def get_console_connection(self, virtual_machine_id: str, mode: str = "tty") -> dict[str, str]:
		"""Return the websocket URL and auth header for a VM console."""
		websocket_url = self.base_url.replace("http://", "ws://", 1)
		return {
			"url": f"{websocket_url}/v1/vms/{quote(virtual_machine_id, safe='')}/console?mode={mode}",
			"authorization": self.headers["Authorization"],
		}

	def put_virtual_machine(self, virtual_machine_id: str, request: dict[str, Any]) -> MetalVirtualMachine:
		"""Store one VM request under its stable Atlas ID."""
		response = self._request(
			"PUT",
			f"/v1/vms/{quote(virtual_machine_id, safe='')}",
			json=request,
			expected_status=202,
			uncertain_on_failure=True,
			timeout=self.create_timeout_seconds,
		)
		return self._virtual_machine(response)

	def get_virtual_machine(self, virtual_machine_id: str) -> MetalVirtualMachine:
		"""Return the desired and observed state for one VM."""
		response = self._request(
			"GET",
			f"/v1/vms/{quote(virtual_machine_id, safe='')}",
			timeout=self.status_timeout_seconds,
			attempts=self.status_attempts,
			budget_seconds=self.status_budget_seconds,
		)
		return self._virtual_machine(response)

	def request_virtual_machine_restart(self, virtual_machine_id: str) -> MetalVirtualMachine:
		"""Store a restart request for one VM."""
		response = self._request(
			"POST",
			f"/v1/vms/{quote(virtual_machine_id, safe='')}/restart",
			expected_status=202,
			uncertain_on_failure=True,
		)
		return self._virtual_machine(response)

	def delete_virtual_machine(self, virtual_machine_id: str) -> MetalVirtualMachine:
		"""Store a removal request for one VM."""
		response = self._request(
			"DELETE",
			f"/v1/vms/{quote(virtual_machine_id, safe='')}",
			expected_status=202,
			uncertain_on_failure=True,
		)
		return self._virtual_machine(response)

	def set_virtual_machine_power_state(self, virtual_machine_id: str, state: str) -> MetalVirtualMachine:
		"""Replace the desired power state for one VM."""
		response = self._request(
			"PUT",
			f"/v1/vms/{quote(virtual_machine_id, safe='')}/power",
			json={"state": state},
			expected_status=202,
			uncertain_on_failure=True,
		)
		return self._virtual_machine(response)

	def replace_virtual_machine_ssh_keys(
		self, virtual_machine_id: str, ssh_keys: list[str]
	) -> MetalVirtualMachine:
		"""Replace all authorized SSH keys for one VM."""
		response = self._request(
			"PUT",
			f"/v1/vms/{quote(virtual_machine_id, safe='')}/ssh-keys",
			json={"ssh_keys": ssh_keys},
			expected_status=(200, 202),
			uncertain_on_failure=True,
		)
		return self._virtual_machine(response)

	def replace_virtual_machine_metadata(
		self, virtual_machine_id: str, metadata: dict[str, str]
	) -> MetalVirtualMachine:
		"""Replace all custom metadata for one VM with a plain string-to-string map."""
		response = self._request(
			"PUT",
			f"/v1/vms/{quote(virtual_machine_id, safe='')}/metadata",
			json={"metadata": metadata},
			expected_status=(200, 202),
			uncertain_on_failure=True,
		)
		return self._virtual_machine(response)

	def set_virtual_machine_network(
		self, virtual_machine_id: str, network: dict[str, Any]
	) -> MetalVirtualMachine:
		"""Replace the complete desired network for one VM."""
		response = self._request(
			"PUT",
			f"/v1/vms/{quote(virtual_machine_id, safe='')}/network",
			json=network,
			expected_status=202,
			uncertain_on_failure=True,
		)
		return self._virtual_machine(response)

	def set_virtual_machine_disk(self, virtual_machine_id: str, disk: dict[str, Any]) -> MetalVirtualMachine:
		"""Replace the complete desired disk for one VM."""
		response = self._request(
			"PUT",
			f"/v1/vms/{quote(virtual_machine_id, safe='')}/disk",
			json=disk,
			expected_status=202,
			uncertain_on_failure=True,
		)
		return self._virtual_machine(response)

	def set_virtual_machine_compute(
		self, virtual_machine_id: str, compute: dict[str, Any]
	) -> MetalVirtualMachine:
		"""Replace the complete desired compute object for one VM."""
		response = self._request(
			"PUT",
			f"/v1/vms/{quote(virtual_machine_id, safe='')}/compute",
			json=compute,
			expected_status=202,
			uncertain_on_failure=True,
		)
		return self._virtual_machine(response)

	def create_snapshot(self, virtual_machine_id: str) -> dict[str, Any]:
		"""Create local image staging for one VM."""
		return self._request(
			"POST",
			f"/v1/vms/{quote(virtual_machine_id, safe='')}/snapshots",
			expected_status=201,
			uncertain_on_failure=True,
			timeout=self.snapshot_timeout_seconds,
		)

	def start_snapshot_upload(self, snapshot_id: str, request: dict[str, Any]) -> None:
		"""Ask Metal to start uploading staged artifacts. Returns at once."""
		self._request(
			"POST",
			f"/v1/snapshots/{quote(snapshot_id, safe='')}/upload",
			json=request,
			expected_status=202,
			uncertain_on_failure=True,
			timeout=self.snapshot_timeout_seconds,
		)

	def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
		"""Get the upload status for one staged snapshot."""
		return self._request(
			"GET",
			f"/v1/snapshots/{quote(snapshot_id, safe='')}",
			timeout=self.snapshot_timeout_seconds,
		)

	def delete_snapshot(self, snapshot_id: str) -> None:
		"""Delete local image staging."""
		self._request(
			"DELETE",
			f"/v1/snapshots/{quote(snapshot_id, safe='')}",
			expected_status=204,
			uncertain_on_failure=True,
			timeout=self.snapshot_timeout_seconds,
		)

	def sync(
		self,
		wireguard_peers: list[dict[str, Any]],
		images: list[dict[str, Any]],
		privileged_vm_addresses: list[str],
		unicast_peers: list[str] | None = None,
	) -> dict[str, Any]:
		"""Exchange controller and host state."""
		request = {
			"wireguard_peers": wireguard_peers,
			"images": images,
			"privileged_vm_addresses": privileged_vm_addresses,
		}
		# Metal decodes the request strictly, so the unicast field travels only in unicast mode.
		if unicast_peers is not None:
			request["unicast_peers"] = unicast_peers
		return self._request("POST", "/v1/sync", json=request, uncertain_on_failure=True)

	def put_migration(self, migration_id: str, virtual_machine_id: str, source: str) -> dict[str, Any]:
		"""Store one migration request at the target host. Safe to repeat."""
		return self._request(
			"PUT",
			f"/v1/migrations/{quote(migration_id, safe='')}",
			json={"virtual_machine_id": virtual_machine_id, "source": source},
			expected_status=202,
			uncertain_on_failure=True,
			timeout=self.create_timeout_seconds,
		)

	def get_migration(self, migration_id: str) -> dict[str, Any]:
		"""Return the status and progress for one migration at the target host."""
		return self._request(
			"GET",
			f"/v1/migrations/{quote(migration_id, safe='')}",
			timeout=self.status_timeout_seconds,
		)

	def abort_migration(self, migration_id: str) -> dict[str, Any]:
		"""Ask the target host to abort one migration. Safe to repeat."""
		return self._request(
			"POST",
			f"/v1/migrations/{quote(migration_id, safe='')}/abort",
			expected_status=202,
			uncertain_on_failure=True,
		)

	def finish_migration(self, migration_id: str) -> dict[str, Any]:
		"""Tell the target host that Atlas committed the VM. Safe to repeat."""
		return self._request(
			"POST",
			f"/v1/migrations/{quote(migration_id, safe='')}/finish",
			expected_status=202,
			uncertain_on_failure=True,
		)

	def _request(
		self,
		method: str,
		path: str,
		*,
		expected_status: int | tuple[int, ...] | None = None,
		uncertain_on_failure: bool = False,
		attempts: int = 1,
		budget_seconds: float | None = None,
		**kwargs: Any,
	) -> dict[str, Any]:
		"""Send one request, and repeat a retryable failure within the caller budget.

		Every attempt shares one deadline, so a repeated call never waits longer
		than a single call would. A caller that reads state while a user waits
		keeps its own latency.
		"""
		timeout = kwargs.pop("timeout", self.timeout_seconds)
		deadline = monotonic() + budget_seconds if budget_seconds else None

		for attempt in range(1, attempts + 1):
			try:
				return self._send(
					method,
					path,
					expected_status=expected_status,
					uncertain_on_failure=uncertain_on_failure,
					timeout=self._attempt_timeout(timeout, deadline),
					**kwargs,
				)
			except MetalClientError as error:
				if not error.retryable or attempt == attempts:
					raise
				if deadline and monotonic() + self.retry_delay_seconds >= deadline:
					raise
				time.sleep(self.retry_delay_seconds)

		raise AssertionError("unreachable")

	@staticmethod
	def _attempt_timeout(timeout: tuple[float, float], deadline: float | None) -> tuple[float, float]:
		"""Return the timeout for one attempt, narrowed to the remaining budget."""
		if deadline is None:
			return timeout

		connect, read = timeout
		return (connect, max(0.1, min(read, deadline - monotonic())))

	def _send(
		self,
		method: str,
		path: str,
		*,
		expected_status: int | tuple[int, ...] | None = None,
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
			message, code, response_retryable = self._error_data(response)
			retryable = (
				response_retryable
				if response_retryable is not None
				else response.status_code in {408, 429} or response.status_code >= 500
			)
			raise MetalClientError(
				message,
				status=response.status_code,
				code=code,
				retryable=retryable,
				uncertain=uncertain_on_failure and retryable,
			)
		expected_statuses = (expected_status,) if isinstance(expected_status, int) else expected_status
		if expected_statuses is not None and response.status_code not in expected_statuses:
			raise MetalClientError(
				f"Metal returned HTTP {response.status_code}, expected {expected_statuses}",
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

	def _virtual_machine(self, response: dict[str, Any]) -> MetalVirtualMachine:
		try:
			return MetalVirtualMachine.from_dict(response)
		except ValueError as error:
			raise MetalClientError("Metal returned an invalid virtual machine response") from error

	@staticmethod
	def _error_data(response: requests.Response) -> tuple[str, str | None, bool | None]:
		try:
			body = response.json()
		except ValueError:
			return f"Metal returned HTTP {response.status_code}", None, None
		if isinstance(body, dict):
			error = body.get("error")
			if isinstance(error, dict):
				message = error.get("message")
				code = error.get("code")
				retryable = error.get("retryable")
				if isinstance(message, str):
					return (
						message,
						code if isinstance(code, str) else None,
						retryable if isinstance(retryable, bool) else None,
					)
		return f"Metal returned HTTP {response.status_code}", None, None
