from __future__ import annotations

import ipaddress
import subprocess
from collections.abc import Callable, Mapping
from time import monotonic, sleep
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, override

import frappe

from atlas.atlas.core.server_providers import register
from atlas.atlas.core.server_providers.base import (
	ProviderOperationError,
	ProviderServer,
	ReservedIPAddress,
	ServerCreateRequest,
	ServerImageData,
	ServerPowerAction,
	ServerProvider,
	ServerSizeData,
)
from atlas.atlas.core.server_providers.scaleway.catalog import ScalewayCatalog
from atlas.atlas.core.server_providers.scaleway.client import ScalewayClient, ScalewayError
from atlas.atlas.core.server_providers.scaleway.configuration import ScalewayConfiguration
from atlas.atlas.core.server_providers.scaleway.infrastructure import ScalewayInfrastructure
from atlas.atlas.core.server_providers.scaleway.ip_addresses import ScalewayIPAddresses
from atlas.atlas.core.server_providers.scaleway.partitioning import ScalewayPartitioning
from atlas.atlas.core.server_providers.scaleway.servers import ScalewayServers

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.server.doctype.server.server import Server

PollResult = TypeVar("PollResult")


@register
class ScalewayProvider(ServerProvider):
	"""Provide Atlas server operations through the Scaleway API."""

	provider_type = "Scaleway"
	credential_fields = ("scaleway_secret_key", "scaleway_access_key")
	private_network_min_prefix = 20
	private_network_max_prefix = 29
	setup_poll_interval_seconds: ClassVar[int] = 5
	setup_poll_timeout_seconds: ClassVar[int] = 7_200
	private_address_attempts: ClassVar[int] = 60
	public_network_interface: ClassVar[str] = "eno1"

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		super().__init__(settings)
		self.configuration = ScalewayConfiguration.from_settings(self.settings)
		self.project_id = self.configuration.project_id
		self.organization_id = self.configuration.organization_id
		self.zone = self.configuration.zone
		self.region = self.configuration.region
		self.resource_name_prefix = self.configuration.resource_name_prefix
		self.client = ScalewayClient(self.settings.get_password("scaleway_secret_key"))
		self.catalog = ScalewayCatalog()
		self.partitioning = ScalewayPartitioning()
		self.infrastructure = ScalewayInfrastructure(self)
		self.servers = ScalewayServers(self.client, self.configuration, self.catalog, self.partitioning)
		self.ip_addresses = ScalewayIPAddresses(self.client, self.configuration)

	@override
	def validate_settings(self) -> None:
		"""Validate the Scaleway infrastructure settings."""
		self.infrastructure.validate_settings()

	@override
	def setup_infrastructure(self) -> None:
		"""Create the Scaleway infrastructure resources."""
		vpc_id = self.infrastructure.create_vpc()
		self.settings.scaleway_vpc_id = vpc_id
		self.settings.db_set("scaleway_vpc_id", vpc_id, update_modified=False)

		private_network_id = self.infrastructure.create_private_network(
			self.settings.private_network_cidr, vpc_id
		)
		self.settings.scaleway_private_network_id = private_network_id
		self.settings.db_set("scaleway_private_network_id", private_network_id, update_modified=False)

		ssh_key_id = self.infrastructure.create_ssh_key(self.settings.public_ssh_key)
		self.settings.scaleway_ssh_key_id = ssh_key_id
		self.settings.db_set("scaleway_ssh_key_id", ssh_key_id, update_modified=False)
		self.settings.is_server_provider_setup_completed = 1
		self.settings.save()

	@override
	def validate_credentials(self) -> bool:
		"""Return true when the Scaleway key is valid."""
		return self.infrastructure.validate_credentials()

	def create_vpc(self) -> str:
		"""Return the Atlas VPC ID, and create the VPC when necessary."""
		return self.infrastructure.create_vpc()

	def create_private_network(self, cidr: str) -> str:
		"""Return the Atlas private network ID, and create the network when necessary."""
		return self.infrastructure.create_private_network(cidr)

	def create_ssh_key(self, public_key: str) -> str:
		"""Return the Atlas SSH key ID, and create the key when necessary."""
		return self.infrastructure.create_ssh_key(public_key)

	@override
	def fetch_server_sizes(self) -> tuple[ServerSizeData, ...]:
		"""Return the Elastic Metal offers for the configured zone."""
		response = self._request("GET", f"/baremetal/v1/zones/{self.zone}/offers?page_size=100")
		return self.catalog.get_server_sizes(response.get("offers", []))

	@override
	def fetch_server_images(self) -> tuple[ServerImageData, ...]:
		"""Return the supported operating system images for the configured zone."""
		response = self._request("GET", f"/baremetal/v1/zones/{self.zone}/os?page_size=100")
		return self.catalog.get_server_images(response.get("os", []))

	@override
	def ensure_server(self, request: ServerCreateRequest) -> ProviderServer:
		"""Return the named Scaleway server, and create it when necessary."""
		return self.servers.ensure(request)

	@override
	def prepare_server(self, server: "Server") -> None:
		"""Prepare the Scaleway server before Secure Shell access."""
		self.attach_private_network(server)
		self.wait_for_private_network(server)
		self.wait_for_server_ready(server)

	@override
	def configure_server_network(self, server: "Server") -> None:
		"""Configure and check the private network through Secure Shell."""
		parent_interface = server.public_network_interface
		device = server.private_network_interface
		if not parent_interface or not device:
			raise ScalewayError("Scaleway server has no public or private network interface")

		self.run_setup_script(
			server,
			"scaleway/configure-private-network.sh",
			environment={
				"PARENT_INTERFACE": parent_interface,
				"DEVICE": device,
				"VLAN": self.private_network_vlan(server),
				"ADDRESS": f"{server.private_ipv4_address}/{self.private_network_prefix_length}",
				"MTU": self.settings.private_network_mtu,
			},
		)
		self.wait_for_private_address(server)

	@override
	def get_storage_pool_device(self, server: "Server") -> str:
		"""Return the raw device for the virtual machine storage pool."""
		return self.partitioning.storage_array

	@override
	def set_power_state(self, provider_server_id: str, action: ServerPowerAction) -> None:
		"""Apply one power action to a Scaleway server."""
		self.servers.set_power_state(provider_server_id, action)

	@override
	def delete_server(self, provider_server_id: str) -> None:
		"""Delete one Scaleway server if it exists."""
		self.servers.delete(provider_server_id)

	@override
	def reserve_public_ipv4_address(self) -> ReservedIPAddress:
		return self.ip_addresses.reserve()

	@override
	def delete_public_ipv4_address(self, provider_resource_id: str) -> None:
		self.ip_addresses.delete(provider_resource_id)

	@override
	def attach_public_ipv4_address(self, provider_resource_id: str, server: "Server") -> None:
		if not server.provider_server_id:
			raise ScalewayError("Atlas server has no Scaleway server ID")
		self.ip_addresses.attach(provider_resource_id, server.provider_server_id)

	@override
	def detach_public_ipv4_address(self, provider_resource_id: str) -> None:
		self.ip_addresses.detach(provider_resource_id)

	def attach_private_network(self, server: "Server") -> None:
		"""Attach the Atlas private network to one Scaleway server."""
		if not server.provider_server_id:
			raise ScalewayError("Atlas server has no Scaleway server ID")
		private_network = self.servers.ensure_private_network(server.provider_server_id)
		vlan = private_network.get("vlan")
		private_network_interface_id = private_network.get("id")
		if not isinstance(vlan, int):
			raise ScalewayError("Scaleway did not return a private network VLAN ID")
		if not isinstance(private_network_interface_id, str):
			raise ScalewayError("Scaleway did not return a private network interface ID")

		server.public_network_interface = self.public_network_interface
		server.private_network_interface = f"{self.public_network_interface}.{vlan}"
		server.private_ipv4_address = self.servers.private_ipv4_address(private_network_interface_id)
		self.update_provider_metadata(server, private_network=private_network)

	def wait_for_private_network(self, server: "Server") -> None:
		"""Wait until Scaleway attaches the private network."""
		if not server.provider_server_id:
			raise ScalewayError("Atlas server has no Scaleway server ID")

		def is_attached() -> Mapping | None:
			private_network = self.servers.private_network(server.provider_server_id)
			if private_network is None:
				raise ScalewayError("Scaleway server is not attached to the Atlas private network")
			if private_network.get("status") == "error":
				raise ScalewayError("Scaleway private network attachment failed")
			self.update_provider_metadata(server, private_network=private_network)
			return private_network if private_network.get("status") == "attached" else None

		self.poll(
			is_attached,
			timeout_seconds=self.setup_poll_timeout_seconds,
			poll_interval_seconds=self.setup_poll_interval_seconds,
			description="the Scaleway private network attachment",
		)

	def wait_for_server_ready(self, server: "Server") -> None:
		"""Wait for the provider server and operating system installation."""
		if not server.provider_server_id:
			raise ScalewayError("Atlas server has no Scaleway server ID")

		def is_ready() -> Mapping | None:
			remote_server = self.servers.fetch(server.provider_server_id)
			self.apply_provider_server(server, self.servers.to_provider_server(remote_server))
			status = remote_server.get("status")
			install_status = (remote_server.get("install") or {}).get("status")
			if status == "error" or install_status == "error":
				raise ScalewayError("Scaleway server provisioning failed")
			if status in {"ready", "running"} and install_status in {None, "completed"}:
				return remote_server
			return None

		self.poll(
			is_ready,
			timeout_seconds=self.setup_poll_timeout_seconds,
			poll_interval_seconds=self.setup_poll_interval_seconds,
			description="the Scaleway server installation",
		)

	def promote_ssh_user(self, server: "Server", user: str) -> None:
		"""Promote the Ubuntu Secure Shell user to root access."""
		if user != "ubuntu":
			raise ScalewayError(f"Scaleway cannot promote Secure Shell user {user}")
		self.run_setup_script(server, "scaleway/promote-ubuntu-user.sh", ssh_user=user)

	def wait_for_private_address(self, server: "Server") -> None:
		"""Wait until the configured private address is available."""
		from atlas.atlas.core.ssh import SSHRunner

		device = server.private_network_interface
		expected_address = server.private_ipv4_address
		if not device or not expected_address:
			raise ScalewayError("Atlas server has no private network interface or address")

		def has_private_address() -> bool | None:
			try:
				result = SSHRunner(server.public_ipv4_address).run_command(
					f"ip -4 -o addr show dev {device} scope global", timeout_seconds=15
				)
			except OSError, subprocess.TimeoutExpired:
				return None
			return True if result.exit_code == 0 and expected_address in result.output else None

		try:
			self.poll(
				has_private_address,
				timeout_seconds=self.private_address_attempts * self.setup_poll_interval_seconds,
				poll_interval_seconds=self.setup_poll_interval_seconds,
				description=f"the private address {expected_address} on {device}",
			)
		except ProviderOperationError as error:
			raise ScalewayError(str(error), is_retryable=True) from error

	def run_setup_script(
		self,
		server: "Server",
		script: str,
		*,
		ssh_user: str = "root",
		environment: Mapping[str, object] | None = None,
		timeout_seconds: int = 120,
	) -> None:
		"""Run one packaged setup script through Secure Shell."""
		from atlas.server.doctype.server_ssh_task.server_ssh_task import ServerSSHTask

		task = ServerSSHTask.create_for_script_file(
			server=server.name,
			script_path=script,
			ssh_user=ssh_user,
			environment=environment,
			timeout_seconds=timeout_seconds,
			run_in_background=False,
		)
		result = task.result
		if result is None or not result.is_success:
			raise ScalewayError(f"Setup script {script} failed", is_retryable=True)

	def poll(
		self,
		operation: Callable[[], PollResult | None],
		*,
		timeout_seconds: int,
		poll_interval_seconds: int,
		description: str,
	) -> PollResult:
		"""Poll one operation until it returns a result or reaches its time limit."""
		deadline = monotonic() + timeout_seconds
		while monotonic() < deadline:
			result = operation()
			if result is not None:
				return result
			sleep(poll_interval_seconds)
		raise ScalewayError(f"Timed out while waiting for {description}", is_retryable=True)

	@property
	def private_network_prefix_length(self) -> int:
		"""Return the prefix length of the Atlas private network."""
		return ipaddress.ip_network(self.settings.private_network_cidr, strict=False).prefixlen

	@staticmethod
	def private_network_vlan(server: "Server") -> int:
		"""Return the VLAN ID that Scaleway assigned to the private network."""
		metadata = frappe.parse_json(server.provider_metadata or "{}")
		private_network = metadata.get("private_network") if isinstance(metadata, Mapping) else None
		vlan = private_network.get("vlan") if isinstance(private_network, Mapping) else None
		if not isinstance(vlan, int):
			raise ScalewayError("Scaleway private network has no VLAN ID")
		return vlan

	@staticmethod
	def apply_provider_server(server: "Server", provider_server: ProviderServer) -> None:
		"""Apply provider-owned values to an Atlas Server."""
		server.provider_server_id = provider_server.provider_server_id
		if provider_server.status:
			server.status = provider_server.status
		server.public_ipv4_address = provider_server.public_ipv4_address
		ScalewayProvider.update_provider_metadata(server, **provider_server.provider_metadata)

	@staticmethod
	def update_provider_metadata(document: "Server", **updates: object) -> None:
		"""Merge provider values into the Server metadata."""
		metadata = frappe.parse_json(document.provider_metadata or "{}")
		if not isinstance(metadata, dict):
			metadata = {}
		metadata.update(updates)
		document.provider_metadata = frappe.as_json(metadata)

	def _request(
		self, method: str, path: str, allow_missing: bool = False, **kwargs: object
	) -> dict[str, Any]:
		"""Send one Scaleway API request."""
		return self.client.request(method, path, allow_missing=allow_missing, **kwargs)
