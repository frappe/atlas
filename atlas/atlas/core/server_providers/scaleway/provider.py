from __future__ import annotations

import ipaddress
import subprocess
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, override

import frappe

from atlas.atlas.core.server_providers import register
from atlas.atlas.core.server_providers.base import ImageInfo, ServerProvider, ServerProviderError, SizeInfo

from .catalog import ScalewayCatalog
from .client import ScalewayClient, ScalewayError
from .infrastructure import ScalewayInfrastructure
from .partitioning import ScalewayPartitioning

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.server.doctype.server.server import Server


@register
class ScalewayProvider(ServerProvider):
	provider_type = "Scaleway"
	private_network_min_prefix = 20
	private_network_max_prefix = 29
	server_status_map: ClassVar[dict[str, str]] = {
		"delivering": "Installing",
		"installing": "Installing",
		"ready": "Installing",
		"running": "Installing",
		"stopped": "Stopped",
		"error": "Failed",
	}
	credential_fields = ("scaleway_secret_key", "scaleway_access_key")
	setup_poll_interval_seconds: ClassVar[int] = 5
	# Scaleway Elastic Metal names the first NIC eno1 and carries the public
	# address on it. The private network arrives tagged on the same NIC.
	public_network_interface: ClassVar[str] = "eno1"
	private_address_attempts: ClassVar[int] = 60
	setup_poll_timeout_seconds: ClassVar[int] = 7_200

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		super().__init__(settings)
		self.project_id = self.settings.scaleway_project_id
		self.zone = self.settings.scaleway_zone
		self.region = self.zone.rsplit("-", 1)[0]
		self.resource_name_prefix = self.settings.resource_name_prefix
		self.organization_id = self.settings.scaleway_organization_id
		self.client = ScalewayClient(self.settings.get_password("scaleway_secret_key"))
		self.catalog = ScalewayCatalog()
		self.partitioning = ScalewayPartitioning()
		self.infrastructure = ScalewayInfrastructure(self)

	@override
	def validate_settings(self) -> None:
		"""Validate the Scaleway infrastructure settings."""
		self.infrastructure.validate_settings()

	@override
	def bootstrap(self) -> None:
		"""Create the Scaleway infrastructure resources."""
		self.infrastructure.bootstrap()

	@override
	def validate_credentials(self) -> bool:
		"""Return true when the Scaleway key is valid."""
		return self.infrastructure.validate_credentials()

	@override
	def create_vpc(self) -> str:
		"""Return the Atlas VPC ID, or create a VPC."""
		return self.infrastructure.create_vpc()

	@override
	def create_private_network(self, cidr: str) -> str:
		"""Return the Atlas private network ID, or create a network."""
		return self.infrastructure.create_private_network(cidr)

	@override
	def create_ssh_key(self, public_key: str) -> str:
		"""Return the Atlas SSH key ID, or create an SSH key."""
		return self.infrastructure.create_ssh_key(public_key)

	@override
	def fetch_server_sizes(self) -> tuple[SizeInfo, ...]:
		"""Return and merge the Elastic Metal offers for the configured zone."""
		response = self._request("GET", f"/baremetal/v1/zones/{self.zone}/offers?page_size=100")
		return self.catalog.get_server_sizes(response.get("offers", []))

	@override
	def fetch_server_images(self) -> tuple[ImageInfo, ...]:
		"""Return the Ubuntu and Debian OS images available in the configured zone."""
		response = self._request("GET", f"/baremetal/v1/zones/{self.zone}/os?page_size=100")
		return self.catalog.get_server_images(response.get("os", []))

	@property
	@override
	def provisioning_steps(self) -> tuple[Callable[["Server"], None], ...]:
		"""Return the Scaleway server provisioning steps."""
		return (
			self._attach_private_network,
			self._wait_for_private_network,
			self._wait_for_server_ready,
			self.wait_for_ssh,
			self._configure_private_network,
			self._wait_for_private_address,
		)

	@override
	def create_server(self, server: "Server") -> None:
		"""Create the Scaleway server before Atlas inserts its Server document."""
		self._create_server(server)

	@override
	def get_storage_pool_device(self, server: "Server") -> str:
		"""Return the raw device for the VM storage pool."""
		return self.partitioning.storage_array

	@override
	def reboot_server(self, server: "Server") -> None:
		"""Reboot the Scaleway server."""
		self._post_server_action(server, "reboot")

	@override
	def poweroff_server(self, server: "Server") -> None:
		"""Stop the Scaleway server."""
		self._post_server_action(server, "stop")

	@override
	def poweron_server(self, server: "Server") -> None:
		"""Start the Scaleway server."""
		self._post_server_action(server, "start")

	def _post_server_action(self, server: "Server", action: str) -> None:
		"""Post one Scaleway power action for a server."""
		if not server.provider_server_id:
			raise ScalewayError("Atlas server has no Scaleway server ID")

		self._request(
			"POST",
			f"/baremetal/v1/zones/{self.zone}/servers/{server.provider_server_id}/{action}",
			json={},
		)

	@override
	def archive_server(self, server: "Server") -> None:
		"""Delete the Scaleway server that backs an Atlas Server."""
		if not server.provider_server_id:
			return

		self._request(
			"DELETE",
			f"/baremetal/v1/zones/{self.zone}/servers/{server.provider_server_id}",
			allow_missing=True,
		)

	@override
	def cleanup_provisioned_server(self, server: "Server") -> None:
		"""Remove the Scaleway server when Atlas cannot create its document."""
		if not getattr(server.flags, "provider_server_created", False):
			return

		server_id = server.provider_server_id
		if not server_id:
			remote_server = self._find_server(server.name)
			server_id = remote_server.get("id") if remote_server else None
		if not isinstance(server_id, str):
			return

		self._request("DELETE", f"/baremetal/v1/zones/{self.zone}/servers/{server_id}")

	def _create_server(self, server: "Server") -> None:
		if server.provider_server_id:
			return

		remote_server = self._find_server(server.name)
		if remote_server is None:
			server.flags.provider_server_created = True
			size_document = frappe.get_doc("Server Size", server.server_size)
			image_document = frappe.get_doc("Server Image", server.server_image)
			offer_id = self.catalog.get_offer_id(size_document, self._subscription_period())
			remote_server = self._request(
				"POST",
				f"/baremetal/v1/zones/{self.zone}/servers",
				json={
					"offer_id": offer_id,
					"option_ids": [self.catalog.get_private_network_option_id(size_document)],
					"project_id": self.project_id,
					"name": server.name,
					"description": f"Atlas server {server.name}",
					"tags": [self._server_tag(server)],
					"install": self._get_install_configuration(server, offer_id, image_document),
				},
			)

		server_id = remote_server.get("id")
		if not isinstance(server_id, str):
			raise ScalewayError("Scaleway did not return a server ID")

		server.provider_server_id = server_id
		self._update_server_details(server, remote_server)

	def _get_install_configuration(self, server: "Server", offer_id: str, image_document: object) -> dict:
		"""Return install settings with custom partitioning when supported."""
		os_id = image_document.get_provider_metadata("id")
		install = {
			"os_id": os_id,
			"hostname": server.name,
			"ssh_key_ids": [self.settings.scaleway_ssh_key_id],
		}

		schema = self._get_partitioning_schema(offer_id, os_id)
		if schema is not None:
			install["partitioning_schema"] = schema
		return install

	def _get_partitioning_schema(self, offer_id: str, os_id: str) -> dict | None:
		"""Return the partitioning schema for an offer and operating system.

		Keep the vendor layout when the endpoint returns 404.
		"""
		default_schema = self._request(
			"GET",
			f"/baremetal/v1/zones/{self.zone}/partitioning-schemas/default",
			allow_missing=True,
			params={"offer_id": offer_id, "os_id": os_id},
		)
		return self.partitioning.get_schema(default_schema)

	def _attach_private_network(self, server: "Server") -> None:
		if not server.provider_server_id:
			raise ScalewayError("Atlas server has no Scaleway server ID")

		private_network = self._server_private_network(server.provider_server_id)
		if private_network is None:
			private_network = self._request(
				"POST",
				f"/baremetal/v1/zones/{self.zone}/servers/{server.provider_server_id}/private-networks",
				json={"private_network_id": self.settings.scaleway_private_network_id},
			)
		vlan = private_network.get("vlan")
		if not isinstance(vlan, int):
			raise ScalewayError("Scaleway did not return a private network VLAN ID")

		private_nic_id = private_network.get("id")
		if not isinstance(private_nic_id, str):
			raise ScalewayError("Scaleway did not return a private network NIC ID")

		server.public_network_interface = self.public_network_interface
		server.private_network_interface = f"{self.public_network_interface}.{vlan}"
		server.private_ipv4_address = self.get_private_ipv4_address(private_nic_id)
		self._update_provider_metadata(server, private_network=private_network)

	@property
	def private_network_prefix_length(self) -> int:
		"""Return the prefix length of the Atlas private network."""
		return ipaddress.ip_network(self.settings.private_network_cidr, strict=False).prefixlen

	def get_private_ipv4_address(self, private_nic_id: str) -> str:
		"""Return the private IPv4 address that Scaleway IPAM assigned to a NIC.

		IPAM holds the address as soon as the NIC attaches, so Atlas does not wait
		for the server to boot before it records the private address. IPAM reports
		the address with its prefix, which Atlas Settings already holds.
		"""
		response = self._request(
			"GET",
			f"/ipam/v1/regions/{self.region}/ips",
			params={"project_id": self.project_id, "resource_id": private_nic_id},
		)
		for ip in response.get("ips", []):
			if ip.get("is_ipv6") or not isinstance(ip.get("address"), str):
				continue
			interface = ipaddress.ip_interface(ip["address"])
			if isinstance(interface, ipaddress.IPv4Interface):
				return str(interface.ip)

		raise ScalewayError(f"Scaleway IPAM has no private IPv4 address for {private_nic_id}")

	def _subscription_period(self) -> str:
		"""Return the Scaleway subscription period selected in Atlas Settings."""
		billing_cycle = self.settings.scaleway_machine_billing_cycle
		if billing_cycle not in {"Hourly", "Monthly"}:
			raise ScalewayError("Scaleway Machine Billing Cycle must be Hourly or Monthly")
		return billing_cycle.lower()

	def _wait_for_private_network(self, server: "Server") -> None:
		if not server.provider_server_id:
			raise ScalewayError("Atlas server has no Scaleway server ID")

		def is_attached() -> Mapping | None:
			private_network = self._server_private_network(server.provider_server_id)
			if private_network is None:
				raise ScalewayError("Scaleway server is not attached to the Atlas private network")
			if private_network.get("status") == "error":
				raise ScalewayError("Scaleway private network attachment failed")

			self._update_provider_metadata(server, private_network=private_network)
			if private_network.get("status") == "attached":
				return private_network
			return None

		self.poll(
			is_attached,
			timeout_seconds=self.setup_poll_timeout_seconds,
			poll_interval_seconds=self.setup_poll_interval_seconds,
			description="the Scaleway private network attachment",
			on_retry=lambda: self.save_server_setup_progress(server),
		)

	def _wait_for_server_ready(self, server: "Server") -> None:
		"""Wait for the server and its OS install.

		Scaleway reports the server as ready while it still installs the OS, so the
		install status decides when the server can accept SSH.
		"""

		def is_ready() -> Mapping | None:
			remote_server = self._fetch_server(server)
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
			on_retry=lambda: self.save_server_setup_progress(server),
		)

	def _fetch_server(self, server: "Server") -> Mapping:
		"""Return the Scaleway server and update the Atlas Server details."""
		if not server.provider_server_id:
			raise ScalewayError("Atlas server has no Scaleway server ID")

		remote_server = self._request(
			"GET", f"/baremetal/v1/zones/{self.zone}/servers/{server.provider_server_id}"
		)
		self._update_server_details(server, remote_server)
		return remote_server

	@override
	def promote_ssh_user(self, server: "Server", user: str) -> None:
		"""Promote the Ubuntu SSH user to root access."""
		if user != "ubuntu":
			raise ScalewayError(f"Scaleway cannot promote SSH user {user}")
		self.run_setup_script(server, "scaleway/promote-ubuntu-user.sh", ssh_user=user)

	def _configure_private_network(self, server: "Server") -> None:
		"""Add the private-network VLAN device and leave the public interface alone."""
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
				"VLAN": self._private_network_vlan(server),
				"ADDRESS": f"{server.private_ipv4_address}/{self.private_network_prefix_length}",
				"MTU": self.settings.private_network_mtu,
			},
		)

	@staticmethod
	def _private_network_vlan(server: "Server") -> int:
		"""Return the VLAN ID that Scaleway assigned to the private network."""
		metadata = frappe.parse_json(server.provider_metadata or "{}")
		private_network = metadata.get("private_network") if isinstance(metadata, Mapping) else None
		vlan = private_network.get("vlan") if isinstance(private_network, Mapping) else None
		if not isinstance(vlan, int):
			raise ScalewayError("Scaleway private network has no VLAN ID")
		return vlan

	def _wait_for_private_address(self, server: "Server") -> None:
		"""Wait for the private address to appear on the server.

		The netplan apply runs detached, so its result is only visible on a new
		connection. This also proves the public address survived the apply.
		"""
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
		except ServerProviderError as error:
			raise ScalewayError(str(error)) from error

	def _find_server(self, server_name: str) -> Mapping | None:
		response = self._request(
			"GET",
			f"/baremetal/v1/zones/{self.zone}/servers",
			params={"project_id": self.project_id, "tags": [self._server_tag_from_name(server_name)]},
		)
		servers = response.get("servers", [])
		if not isinstance(servers, list) or not all(isinstance(item, Mapping) for item in servers):
			raise ScalewayError("Scaleway response has invalid servers")
		if len(servers) > 1:
			raise ScalewayError(f"Scaleway returned multiple servers for Atlas server {server_name}")
		return servers[0] if servers else None

	def _server_private_network(self, server_id: str) -> Mapping | None:
		response = self._request(
			"GET",
			f"/baremetal/v1/zones/{self.zone}/server-private-networks",
			params={
				"server_id": server_id,
				"private_network_id": self.settings.scaleway_private_network_id,
			},
		)
		private_networks = response.get("server_private_networks", [])
		if not isinstance(private_networks, list) or not all(
			isinstance(item, Mapping) for item in private_networks
		):
			raise ScalewayError("Scaleway response has invalid server private networks")
		return next(
			(
				private_network
				for private_network in private_networks
				if private_network.get("private_network_id") == self.settings.scaleway_private_network_id
			),
			None,
		)

	def _update_server_details(self, server: "Server", remote_server: Mapping) -> None:
		status = self.server_status_map.get(remote_server.get("status"))
		if status:
			server.status = status
		server.public_ipv4_address = self._public_ipv4_address(remote_server.get("ips", []))
		self._update_provider_metadata(server, server=remote_server)

	@staticmethod
	def _server_tag(server: "Server") -> str:
		return ScalewayProvider._server_tag_from_name(server.name)

	@staticmethod
	def _server_tag_from_name(server_name: str) -> str:
		return f"atlas-server:{server_name}"

	@staticmethod
	def _update_provider_metadata(document: "Server", **updates: object) -> None:
		metadata = frappe.parse_json(document.provider_metadata or "{}")
		if not isinstance(metadata, dict):
			metadata = {}
		metadata.update(updates)
		document.provider_metadata = frappe.as_json(metadata)

	@staticmethod
	def _public_ipv4_address(ips: list[Mapping]) -> str | None:
		for ip in ips:
			if ip.get("version") == "IPv4":
				return ip.get("address")
		return None

	def _request(
		self, method: str, path: str, allow_missing: bool = False, **kwargs: object
	) -> dict[str, Any]:
		"""Send one Scaleway API request.

		Keep this wrapper so provider tests and subclasses can replace transport.
		"""
		return self.client.request(method, path, allow_missing=allow_missing, **kwargs)
