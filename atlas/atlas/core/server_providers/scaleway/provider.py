from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar, override

import frappe

from atlas.atlas.core.server_providers import register
from atlas.atlas.core.server_providers.base import (
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
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


@register
class ScalewayProvider(ServerProvider):
	"""Provide Atlas server operations through the Scaleway API."""

	public_ipv6_prefix_length: ClassVar[int | None] = 64

	provider_type = "Scaleway"
	credential_fields = ("scaleway_secret_key", "scaleway_access_key")
	error_class = ScalewayError
	private_network_min_prefix = 20
	private_network_max_prefix = 29
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
		"""Return the Scaleway server for the identity key."""
		return self.servers.ensure(request)

	@override
	def import_server(self, server: "MetalServer") -> None:
		"""Match an existing Elastic Metal server to the catalog and give it the private network option."""
		remote_server = self.servers.fetch(server.provider_server_id)
		operating_system_id = (remote_server.get("install") or {}).get("os_id")
		if not remote_server.get("offer_id") or not operating_system_id:
			raise ScalewayError(
				f"Scaleway server {server.provider_server_id} has no offer or operating system"
			)
		server.server_size, subscription_period = self.find_offer(remote_server.get("offer_id"))
		server.server_image = self.find_catalog_record(
			"Metal Server Image",
			lambda metadata: metadata.get("id") == operating_system_id,
			f"operating system {operating_system_id}",
		)
		server.title = remote_server.get("name") or server.provider_server_id
		size = frappe.get_doc("Metal Server Size", server.server_size)
		self.servers.ensure_private_network_option(
			remote_server, self.catalog.get_private_network_option_id(size, subscription_period)
		)
		self.apply_provider_server(server, self.servers.to_provider_server(remote_server))

	def find_offer(self, offer_id: object) -> tuple[str, str]:
		"""Return the Metal Server Size and the subscription period of one Scaleway offer."""
		for name, metadata in frappe.get_all(
			"Metal Server Size", fields=["name", "provider_metadata"], as_list=True
		):
			values = frappe.parse_json(metadata or "{}")
			for period, offer in values.items() if isinstance(values, Mapping) else ():
				if not isinstance(offer, Mapping):
					continue
				if offer_id == offer.get("monthly_offer_id"):
					return name, "monthly"
				if offer_id == offer.get("id"):
					return name, period
		raise ScalewayError(
			f"No Metal Server Size matches offer {offer_id}. Sync the Metal Server Size catalog first."
		)

	@override
	def prepare_server(self, server: "MetalServer") -> None:
		"""Prepare the Scaleway server before Secure Shell access."""
		self.attach_private_network(server)
		self.wait_for_private_network(server)
		self.wait_for_server_ready(server)

	@override
	def configure_server_network(self, server: "MetalServer") -> None:
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
	def storage_pool_device(self, server: "MetalServer") -> str:
		"""Return the raw device for the virtual machine storage pool."""
		return self.partitioning.storage_array

	@override
	def set_power_state(self, provider_server_id: str, action: ServerPowerAction) -> None:
		"""Apply one power action to a Scaleway server."""
		self.servers.set_power_state(provider_server_id, action)

	@override
	def delete_server(self, provider_server_id: str, _provider_metadata: Mapping[str, object]) -> None:
		"""Delete one Scaleway server if it exists."""
		self.servers.delete(provider_server_id)

	@override
	def reserve_public_ip_address(self, version: int) -> ReservedIPAddress:
		"""Reserve one public IPv4 address or IPv6 block."""
		return self.ip_addresses.reserve(version)

	@override
	def delete_public_ip_address(self, provider_resource_id: str) -> None:
		"""Release one Flexible IP."""
		self.ip_addresses.delete(provider_resource_id)

	@override
	def attach_public_ip_address(
		self, provider_resource_id: str, public_address: str, server: "MetalServer"
	) -> str:
		"""Attach a reserved address to a server."""
		if not server.provider_server_id:
			raise ScalewayError("Atlas server has no Scaleway server ID")
		self.ip_addresses.attach(provider_resource_id, server.provider_server_id)
		return public_address

	@override
	def detach_public_ip_address(
		self, provider_resource_id: str, host_address: str | None, server: "MetalServer"
	) -> None:
		"""Detach an address from a server."""
		self.ip_addresses.detach(provider_resource_id)

	def attach_private_network(self, server: "MetalServer") -> None:
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

	def wait_for_private_network(self, server: "MetalServer") -> None:
		"""Wait until Scaleway attaches the private network."""
		if not server.provider_server_id:
			raise ScalewayError("Atlas server has no Scaleway server ID")

		def is_attached() -> Mapping | None:
			"""Report whether the address is attached to a server."""
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

	def wait_for_server_ready(self, server: "MetalServer") -> None:
		"""Wait for the provider server and operating system installation."""
		if not server.provider_server_id:
			raise ScalewayError("Atlas server has no Scaleway server ID")

		def is_ready() -> Mapping | None:
			"""Report whether the server has finished provider provisioning."""
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

	def promote_ssh_user(self, server: "MetalServer", user: str) -> None:
		"""Promote the Ubuntu Secure Shell user to root access."""
		if user != "ubuntu":
			raise ScalewayError(f"Scaleway cannot promote Secure Shell user {user}")
		self.run_setup_script(server, "promote-ssh-user.sh", ssh_user=user)

	@staticmethod
	def private_network_vlan(server: "MetalServer") -> int:
		"""Return the VLAN ID that Scaleway assigned to the private network."""
		metadata = frappe.parse_json(server.provider_metadata or "{}")
		private_network = metadata.get("private_network") if isinstance(metadata, Mapping) else None
		vlan = private_network.get("vlan") if isinstance(private_network, Mapping) else None
		if not isinstance(vlan, int):
			raise ScalewayError("Scaleway private network has no VLAN ID")
		return vlan

	def _request(
		self, method: str, path: str, allow_missing: bool = False, **kwargs: object
	) -> dict[str, Any]:
		"""Send one Scaleway API request."""
		return self.client.request(method, path, allow_missing=allow_missing, **kwargs)
