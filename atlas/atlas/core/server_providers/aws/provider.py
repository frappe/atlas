from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, ClassVar, override

import frappe

from atlas.atlas.core.server_providers import register
from atlas.atlas.core.server_providers.aws.catalog import IMAGE_OWNERS, AwsCatalog
from atlas.atlas.core.server_providers.aws.client import AwsClient, AwsError
from atlas.atlas.core.server_providers.aws.configuration import AwsConfiguration
from atlas.atlas.core.server_providers.aws.infrastructure import AwsInfrastructure
from atlas.atlas.core.server_providers.aws.ip_addresses import AwsIPAddresses, AwsIPv6Prefixes
from atlas.atlas.core.server_providers.aws.servers import AwsServers
from atlas.atlas.core.server_providers.aws.volumes import AwsVolumes
from atlas.atlas.core.server_providers.base import (
	ProviderServer,
	ReservedIPAddress,
	ServerCreateRequest,
	ServerImageData,
	ServerPowerAction,
	ServerProvider,
	ServerSizeData,
)

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


@register
class AwsProvider(ServerProvider):
	"""Provide Atlas server operations through the AWS API."""

	public_ipv6_prefix_length: ClassVar[int | None] = 80

	provider_type = "AWS"
	credential_fields = ("aws_secret_access_key", "aws_access_key_id")
	error_class = AwsError
	ssh_users = ("ubuntu", "admin", "root")
	private_network_min_prefix = 16
	private_network_max_prefix = 28
	# AWS gives no stable guest device name, so the setup script renames the mesh
	# interface by its MAC address. metald then uses this name as its mesh uplink.
	private_network_interface: ClassVar[str] = "atlas-mesh"

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		super().__init__(settings)
		self.configuration = AwsConfiguration.from_settings(self.settings)
		self.region = self.configuration.region
		self.client = AwsClient(
			self.settings.aws_access_key_id,
			self.settings.get_password("aws_secret_access_key"),
			self.configuration.region,
		)
		self.catalog = AwsCatalog()
		self.infrastructure = AwsInfrastructure(self)
		self.servers = AwsServers(self.client, self.configuration, self.catalog)
		self.ip_addresses = AwsIPAddresses(self.client, self.configuration)
		self.ipv6_prefixes = AwsIPv6Prefixes(self.client, self.configuration)
		self.volumes = AwsVolumes(self)

	@override
	def validate_settings(self) -> None:
		"""Validate the AWS infrastructure settings."""
		self.infrastructure.validate_settings()

	@override
	def validate_credentials(self) -> bool:
		"""Return true when the AWS keys are valid."""
		return self.infrastructure.validate_credentials()

	@override
	def setup_infrastructure(self) -> None:
		"""Create the AWS infrastructure resources."""
		vpc_id = self.infrastructure.create_vpc()
		self.store("aws_vpc_id", vpc_id)
		self.infrastructure.create_internet_access(vpc_id)

		subnet_id = self.infrastructure.create_subnet(vpc_id)
		self.store("aws_subnet_id", subnet_id)
		self.store("aws_security_group_id", self.infrastructure.create_security_group(vpc_id))
		self.store("aws_key_pair_name", self.infrastructure.create_key_pair(self.settings.public_ssh_key))

		self.settings.is_server_provider_setup_completed = 1
		self.settings.save()

	@override
	def fetch_server_sizes(self) -> tuple[ServerSizeData, ...]:
		"""Return the instance types that can run Atlas virtual machines."""
		instance_types = self.client.paginate(
			"ec2",
			"describe_instance_types",
			"InstanceTypes",
			Filters=[{"Name": "processor-info.supported-architecture", "Values": ["x86_64"]}],
		)
		return self.catalog.get_server_sizes(instance_types)

	@override
	def fetch_server_images(self) -> tuple[ServerImageData, ...]:
		"""Return the supported operating system images for the configured region."""
		images = self.client.paginate(
			"ec2",
			"describe_images",
			"Images",
			Owners=sorted(set(IMAGE_OWNERS.values())),
			Filters=[
				{"Name": "architecture", "Values": ["x86_64"]},
				{"Name": "state", "Values": ["available"]},
				{"Name": "root-device-type", "Values": ["ebs"]},
			],
		)
		return self.catalog.get_server_images(images)

	@override
	def ensure_server(self, request: ServerCreateRequest) -> ProviderServer:
		"""Return the named AWS instance, and create it when necessary."""
		return self.servers.ensure(request)

	@override
	def import_server(self, server: "MetalServer") -> None:
		"""Match an existing instance to the catalog. Its storage volume must exist already."""
		instance = self.servers.fetch(server.provider_server_id)
		if not instance.get("ImageId"):
			raise AwsError(f"AWS instance {server.provider_server_id} has no image")
		# Public addresses attach to the primary interface, so it must be in the Atlas subnet.
		if instance.get("SubnetId") != self.configuration.subnet_id:
			raise AwsError(
				f"AWS instance {server.provider_server_id} is in subnet {instance.get('SubnetId')}, "
				f"not the Atlas subnet {self.configuration.subnet_id}"
			)
		instance_type = instance.get("InstanceType")
		if not isinstance(instance_type, str) or not frappe.db.exists("Metal Server Size", instance_type):
			raise AwsError(
				f"No Metal Server Size matches {instance_type}. Sync the Metal Server Size catalog first."
			)
		server.server_size = instance_type
		# The catalog keeps only the newest image of each version, so match the version.
		images = self.client.call("ec2", "describe_images", ImageIds=[instance["ImageId"]]).get("Images", [])
		versions = self.catalog.get_server_images(images)
		if not versions or not frappe.db.exists("Metal Server Image", versions[0].name):
			raise AwsError(
				f"No Metal Server Image matches image {instance['ImageId']}. Sync the Metal Server Image catalog first."
			)
		server.server_image = versions[0].name
		self.apply_provider_server(server, self.servers.to_provider_server(instance))

	@override
	def prepare_server(self, server: "MetalServer") -> None:
		"""Prepare the AWS instance before Secure Shell access."""
		self.wait_for_server_ready(server)
		self.attach_mesh_interface(server)

	@override
	def configure_server_network(self, server: "MetalServer") -> None:
		"""Configure and check the mesh interface through Secure Shell."""
		device = server.private_network_interface
		if not device:
			raise AwsError("Atlas server has no private network interface")

		server.public_network_interface = self.uplink_interface(server)
		self.run_setup_script(
			server,
			"aws/configure-private-network.sh",
			environment={
				"DEVICE": device,
				"MAC_ADDRESS": self.mesh_mac_address(server),
				"ADDRESS": f"{server.private_ipv4_address}/{self.private_network_prefix_length}",
				"MTU": self.settings.private_network_mtu,
			},
		)
		self.wait_for_private_address(server)

	@override
	def storage_pool_device(self, server: "MetalServer") -> str:
		"""Return the stable device path of the EBS volume for the storage pool."""
		volume_id = self.volumes.volume_id(server, "storage")
		# The NVMe serial of an EBS volume is its id without the dash.
		return f"/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_{volume_id.replace('-', '')}"

	@override
	def set_power_state(self, provider_server_id: str, action: ServerPowerAction) -> None:
		"""Apply one power action to an AWS instance."""
		self.servers.set_power_state(provider_server_id, action)

	@override
	def delete_server(self, provider_server_id: str, provider_metadata: Mapping[str, object]) -> None:
		"""Delete one AWS instance and its owned resources if they exist."""
		interface = provider_metadata.get("mesh_interface")
		interface_id = interface.get("NetworkInterfaceId") if isinstance(interface, Mapping) else None
		self.servers.delete(
			provider_server_id,
			interface_id if isinstance(interface_id, str) else None,
		)

	@override
	def reserve_public_ip_address(self, version: int) -> ReservedIPAddress:
		"""Reserve one Elastic IP address, or choose one free IPv6 /80 block."""
		if version == 6:
			used = set(
				frappe.get_all("Public IP Pool", filters={"version": "6"}, pluck="provider_resource_id")
			)
			return self.ipv6_prefixes.reserve(used)
		return self.ip_addresses.reserve()

	@override
	def delete_public_ip_address(self, provider_resource_id: str) -> None:
		"""Release one Elastic IP address. An IPv6 block has no AWS resource while it is detached."""
		if not is_ipv6_prefix(provider_resource_id):
			self.ip_addresses.delete(provider_resource_id)

	@override
	def attach_public_ip_address(
		self, provider_resource_id: str, public_address: str, server: "MetalServer"
	) -> str:
		"""Attach an address and return its host address. A block returns itself, because the host routes it."""
		interface_id = self.primary_network_interface_id(server)
		if is_ipv6_prefix(provider_resource_id):
			self.ipv6_prefixes.attach(provider_resource_id, interface_id)
			return provider_resource_id
		return self.ip_addresses.attach(provider_resource_id, interface_id)

	@staticmethod
	def primary_network_interface_id(server: "MetalServer") -> str:
		"""Return the interface that carries public traffic for one server."""
		metadata = frappe.parse_json(server.provider_metadata or "{}")
		instance = metadata.get("instance") if isinstance(metadata, Mapping) else None
		interfaces = instance.get("NetworkInterfaces") if isinstance(instance, Mapping) else []
		for interface in interfaces if isinstance(interfaces, list) else []:
			if not isinstance(interface, Mapping):
				continue
			attachment = interface.get("Attachment")
			index = attachment.get("DeviceIndex") if isinstance(attachment, Mapping) else None
			identifier = interface.get("NetworkInterfaceId")
			if index == 0 and isinstance(identifier, str):
				return identifier
		raise AwsError("Atlas server has no AWS primary network interface")

	@override
	def detach_public_ip_address(
		self, provider_resource_id: str, host_address: str | None, server: "MetalServer"
	) -> None:
		"""Detach an address from a server."""
		interface_id = self.primary_network_interface_id(server)
		if is_ipv6_prefix(provider_resource_id):
			self.ipv6_prefixes.detach(provider_resource_id, interface_id)
			return
		self.ip_addresses.detach(provider_resource_id, interface_id, host_address)

	@override
	def promote_ssh_user(self, server: "MetalServer", user: str) -> None:
		"""Promote a supported AWS Secure Shell user to root access."""
		if user not in {"admin", "ubuntu"}:
			raise AwsError(f"AWS cannot promote Secure Shell user {user}")
		self.run_setup_script(server, "promote-ssh-user.sh", ssh_user=user)

	def attach_mesh_interface(self, server: "MetalServer") -> None:
		"""Attach the mesh network interface and let it receive discovery traffic."""
		if not server.provider_server_id:
			raise AwsError("Atlas server has no AWS instance ID")

		interface = self.servers.ensure_mesh_interface(server.provider_server_id, server.name)
		interface_id = interface.get("NetworkInterfaceId")
		if not isinstance(interface_id, str):
			raise AwsError("AWS did not return a mesh network interface ID")
		self.update_provider_metadata(server, mesh_interface=dict(interface))
		self.servers.attach_mesh_interface(interface_id, server.provider_server_id)

		def attached_interface() -> Mapping | None:
			current = self.servers.fetch_mesh_interface(interface_id)
			attachment = current.get("Attachment", {})
			attached_instance = attachment.get("InstanceId")
			if attached_instance and attached_instance != server.provider_server_id:
				raise AwsError(f"AWS mesh interface {interface_id} belongs to another instance")
			status = attachment.get("Status")
			if status == "attached":
				return current
			if status not in {None, "attaching"}:
				raise AwsError(f"AWS reported attachment state {status} for mesh interface {interface_id}")
			return None

		interface = self.poll(
			attached_interface,
			timeout_seconds=self.setup_poll_timeout_seconds,
			poll_interval_seconds=self.setup_poll_interval_seconds,
			description=f"AWS mesh interface {interface_id}",
		)
		self.servers.configure_mesh_interface(interface)
		private_address = interface.get("PrivateIpAddress")
		if not isinstance(private_address, str):
			raise AwsError("AWS did not return a private IPv4 address for the mesh interface")

		server.private_network_interface = self.private_network_interface
		server.private_ipv4_address = private_address
		self.update_provider_metadata(server, mesh_interface=dict(interface))

	def wait_for_server_ready(self, server: "MetalServer") -> None:
		"""Wait until the AWS instance runs and passes both status checks."""
		if not server.provider_server_id:
			raise AwsError("Atlas server has no AWS instance ID")

		def is_ready() -> Mapping | None:
			"""Report whether the instance finished provider provisioning."""
			instance = self.servers.fetch(server.provider_server_id)
			self.apply_provider_server(server, self.servers.to_provider_server(instance))
			state = instance.get("State", {}).get("Name")
			if state in {"shutting-down", "terminated"}:
				raise AwsError(f"AWS instance {server.provider_server_id} is {state}")
			if state != "running":
				return None
			return instance if self.servers.is_ready(server.provider_server_id) else None

		self.poll(
			is_ready,
			timeout_seconds=self.setup_poll_timeout_seconds,
			poll_interval_seconds=self.setup_poll_interval_seconds,
			description="the AWS instance status checks",
		)

	def uplink_interface(self, server: "MetalServer") -> str:
		"""Return the guest device name that carries the AWS default route."""
		from atlas.atlas.core.ssh import SSHRunner

		result = SSHRunner(server.ssh_host).run_command("ip -4 -o route show default", timeout_seconds=15)
		fields = result.output.split()
		if result.exit_code != 0 or "dev" not in fields:
			raise AwsError(f"Atlas server {server.name} has no default route device")
		return fields[fields.index("dev") + 1]

	def store(self, field: str, value: str) -> None:
		"""Store one created provider resource ID on Atlas Settings."""
		setattr(self.settings, field, value)
		self.settings.db_set(field, value, update_modified=False)

	@staticmethod
	def mesh_mac_address(server: "MetalServer") -> str:
		"""Return the MAC address that AWS assigned to the mesh interface."""
		metadata = frappe.parse_json(server.provider_metadata or "{}")
		interface = metadata.get("mesh_interface") if isinstance(metadata, Mapping) else None
		mac_address = interface.get("MacAddress") if isinstance(interface, Mapping) else None
		if not isinstance(mac_address, str):
			raise AwsError("Atlas server has no AWS mesh interface MAC address")
		return mac_address


def is_ipv6_prefix(provider_resource_id: str) -> bool:
	"""Report whether a provider resource is an IPv6 block. An Elastic IP allocation ID holds no colon."""
	return ":" in provider_resource_id
