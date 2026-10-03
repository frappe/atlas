from __future__ import annotations

import ipaddress

from atlas.atlas.core.server_providers.aws.client import AwsClient, AwsError
from atlas.atlas.core.server_providers.aws.configuration import AwsConfiguration
from atlas.atlas.core.server_providers.aws.servers import AwsServers
from atlas.atlas.core.server_providers.base import ReservedIPAddress


class AwsIPAddresses:
	"""Own AWS Elastic IP address operations."""

	def __init__(self, client: AwsClient, configuration: AwsConfiguration) -> None:
		self.client = client
		self.configuration = configuration

	def reserve(self) -> ReservedIPAddress:
		"""Reserve one public IPv4 address."""
		response = self.client.call(
			"ec2",
			"allocate_address",
			Domain="vpc",
			TagSpecifications=[
				{
					"ResourceType": "elastic-ip",
					"Tags": [{"Key": "Name", "Value": f"{self.configuration.resource_name_prefix}address"}],
				}
			],
		)
		address = response.get("PublicIp")
		provider_resource_id = response.get("AllocationId")
		if not isinstance(address, str) or not isinstance(provider_resource_id, str):
			raise AwsError("AWS did not return an Elastic IP address and allocation ID")

		return ReservedIPAddress(address=address, provider_resource_id=provider_resource_id)

	def delete(self, provider_resource_id: str) -> None:
		"""Release one public IPv4 address if it exists."""
		self.client.call("ec2", "release_address", AllocationId=provider_resource_id, allow_missing=True)

	def ensure_host_address(self, server_name: str) -> str:
		"""Return the allocation ID of the host Elastic IP, and allocate it when absent. The tag makes a retry safe."""
		addresses = self.client.call(
			"ec2",
			"describe_addresses",
			Filters=[{"Name": f"tag:{AwsServers.identity_tag_key}", "Values": [server_name]}],
		).get("Addresses", [])
		if len(addresses) > 1:
			raise AwsError(f"AWS returned multiple Elastic IP addresses for Atlas server {server_name}")
		if addresses:
			return addresses[0]["AllocationId"]

		response = self.client.call(
			"ec2",
			"allocate_address",
			Domain="vpc",
			TagSpecifications=[
				{
					"ResourceType": "elastic-ip",
					"Tags": [
						{"Key": "Name", "Value": server_name},
						{"Key": AwsServers.identity_tag_key, "Value": server_name},
					],
				}
			],
		)
		allocation_id = response.get("AllocationId")
		if not isinstance(allocation_id, str):
			raise AwsError(f"AWS did not return an Elastic IP allocation ID for Atlas server {server_name}")
		return allocation_id

	def associate_host_address(self, allocation_id: str, network_interface_id: str) -> None:
		"""Associate the host Elastic IP with the primary private address of an interface."""
		primary_address = self.primary_private_address(network_interface_id)
		association = self.association(allocation_id)
		if (
			association.get("NetworkInterfaceId") == network_interface_id
			and association.get("PrivateIpAddress") == primary_address
		):
			return

		self.client.call(
			"ec2",
			"associate_address",
			AllocationId=allocation_id,
			NetworkInterfaceId=network_interface_id,
			PrivateIpAddress=primary_address,
			AllowReassociation=False,
		)

	def release_host_address(self, allocation_id: str) -> None:
		"""Disassociate and release the host Elastic IP if it exists."""
		association_id = self.association(allocation_id).get("AssociationId")
		if association_id:
			self.client.call("ec2", "disassociate_address", AssociationId=association_id, allow_missing=True)
		self.delete(allocation_id)

	def attach(self, provider_resource_id: str, network_interface_id: str) -> str:
		"""Attach one public address to a dedicated host address."""
		host_address = self.reusable_host_address(provider_resource_id, network_interface_id)
		is_new_address = host_address is None
		if is_new_address:
			host_address = self.assign_host_address(network_interface_id)

		try:
			self.client.call(
				"ec2",
				"associate_address",
				AllocationId=provider_resource_id,
				NetworkInterfaceId=network_interface_id,
				PrivateIpAddress=host_address,
				AllowReassociation=True,
			)
		except Exception as error:
			if is_new_address:
				try:
					self.unassign_host_address(network_interface_id, host_address)
				except Exception as cleanup_error:
					raise AwsError(
						f"Could not clean up private address {host_address}: {cleanup_error}"
					) from error
			raise

		return host_address

	def reusable_host_address(self, provider_resource_id: str, network_interface_id: str) -> str | None:
		"""Return the existing non-primary host address, if one exists."""
		association = self.association(provider_resource_id)
		if association.get("NetworkInterfaceId") != network_interface_id:
			return None

		host_address = association.get("PrivateIpAddress")
		if not host_address or host_address == self.primary_private_address(network_interface_id):
			return None
		return host_address

	def primary_private_address(self, network_interface_id: str) -> str:
		"""Return the private address that belongs to the interface itself."""
		response = self.client.call(
			"ec2", "describe_network_interfaces", NetworkInterfaceIds=[network_interface_id]
		)
		for interface in response.get("NetworkInterfaces") or []:
			for entry in interface.get("PrivateIpAddresses") or []:
				address = entry.get("PrivateIpAddress")
				if entry.get("Primary") and isinstance(address, str):
					return address
		raise AwsError(f"AWS interface {network_interface_id} has no primary private address")

	def assign_host_address(self, network_interface_id: str) -> str:
		"""Add one private address to an interface and return it."""
		response = self.client.call(
			"ec2",
			"assign_private_ip_addresses",
			NetworkInterfaceId=network_interface_id,
			SecondaryPrivateIpAddressCount=1,
		)
		assigned = response.get("AssignedPrivateIpAddresses") or []
		address = assigned[0].get("PrivateIpAddress") if assigned else None
		if not isinstance(address, str):
			raise AwsError(f"AWS assigned no private address to interface {network_interface_id}")
		return address

	def detach(self, provider_resource_id: str, network_interface_id: str, host_address: str | None) -> None:
		"""Detach one public IPv4 address and remove the private address it used."""
		association = self.association(provider_resource_id)
		if not host_address:
			associated_interface = association.get("NetworkInterfaceId")
			host_address = association.get("PrivateIpAddress")
			if associated_interface != network_interface_id or not host_address:
				raise AwsError("AWS public address has no host address on the expected interface")

		if host_address == self.primary_private_address(network_interface_id):
			raise AwsError(f"Refusing to unassign primary private address {host_address}")

		if association.get("AssociationId"):
			self.client.call(
				"ec2",
				"disassociate_address",
				AssociationId=association["AssociationId"],
				allow_missing=True,
			)

		self.unassign_host_address(network_interface_id, host_address)

	def unassign_host_address(self, network_interface_id: str, host_address: str) -> None:
		"""Remove one secondary private address from an interface."""
		self.client.call(
			"ec2",
			"unassign_private_ip_addresses",
			NetworkInterfaceId=network_interface_id,
			PrivateIpAddresses=[host_address],
			allow_missing=True,
		)

	def association(self, provider_resource_id: str) -> dict[str, str]:
		"""Return the current association of one reserved address."""
		response = self.client.call(
			"ec2", "describe_addresses", AllocationIds=[provider_resource_id], allow_missing=True
		)
		for address in response.get("Addresses") or []:
			return {key: value for key, value in address.items() if isinstance(value, str)}
		return {}


class AwsIPv6Prefixes:
	"""Own the /80 IPv6 blocks that AWS delegates to a host interface.

	AWS keeps no reservation. A block is a free /80 of the Atlas subnet, and it moves between hosts because every host is in that subnet.
	"""

	PREFIX_LENGTH = 80

	def __init__(self, client: AwsClient, configuration: AwsConfiguration) -> None:
		self.client = client
		self.configuration = configuration

	def reserve(self, used_prefixes: set[str]) -> ReservedIPAddress:
		"""Return the first /80 of the subnet that no Atlas record uses."""
		block = self.subnet_block()
		for prefix in block.subnets(new_prefix=self.PREFIX_LENGTH):
			# AWS reserves the first four and the last address of a subnet, so it refuses the blocks that hold them.
			if block.network_address in prefix or block.broadcast_address in prefix:
				continue
			if str(prefix) not in used_prefixes:
				return ReservedIPAddress(address=str(prefix), provider_resource_id=str(prefix))
		raise AwsError("The Atlas subnet has no free IPv6 /80 block")

	def subnet_block(self) -> ipaddress.IPv6Network:
		"""Return the IPv6 /64 of the Atlas subnet."""
		subnets = self.client.call("ec2", "describe_subnets", SubnetIds=[self.configuration.subnet_id]).get(
			"Subnets", []
		)
		for association in subnets[0].get("Ipv6CidrBlockAssociationSet", []) if subnets else []:
			if association.get("Ipv6CidrBlockState", {}).get("State") == "associated":
				return ipaddress.IPv6Network(association["Ipv6CidrBlock"])
		raise AwsError("The Atlas subnet has no IPv6 block")

	def attach(self, prefix: str, network_interface_id: str) -> None:
		"""Delegate the block to a host interface."""
		self.client.call(
			"ec2", "assign_ipv6_addresses", NetworkInterfaceId=network_interface_id, Ipv6Prefixes=[prefix]
		)

	def detach(self, prefix: str, network_interface_id: str) -> None:
		"""Take the block back from a host interface."""
		self.client.call(
			"ec2",
			"unassign_ipv6_addresses",
			NetworkInterfaceId=network_interface_id,
			Ipv6Prefixes=[prefix],
			allow_missing=True,
		)
