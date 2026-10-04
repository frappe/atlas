from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING

from atlas.atlas.core.server_providers.aws.client import AwsError

if TYPE_CHECKING:
	from atlas.atlas.core.server_providers.aws.provider import AwsProvider

PRIVATE_IPV4_NETWORKS = tuple(
	ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class AwsInfrastructure:
	"""Manage the AWS resources shared by Atlas servers."""

	def __init__(self, provider: "AwsProvider") -> None:
		self.provider = provider

	def validate_settings(self) -> None:
		"""Validate the AWS region, zone, and private network settings."""
		# A VPC does not carry link-local multicast, so WG Mesh must send NDP to each peer.
		if not self.provider.settings.is_unicast_network_enabled:
			raise AwsError("AWS needs unicast networking. Enable Use Unicast Networking in Atlas Settings.")

		configuration = self.provider.configuration
		if not re.fullmatch(rf"{re.escape(configuration.region)}[a-z]", configuration.availability_zone):
			raise AwsError(
				f"AWS Availability Zone {configuration.availability_zone} is not in region {configuration.region}"
			)

		try:
			network = ipaddress.ip_network(self.provider.settings.private_network_cidr, strict=False)
		except ValueError as error:
			raise AwsError(
				f"Invalid Atlas private network CIDR: {self.provider.settings.private_network_cidr}"
			) from error

		if network.version != 4 or not any(network.subnet_of(item) for item in PRIVATE_IPV4_NETWORKS):
			raise AwsError(f"Atlas private network CIDR {network} must be a private IPv4 network")

		if not (
			self.provider.private_network_min_prefix
			<= network.prefixlen
			<= self.provider.private_network_max_prefix
		):
			raise AwsError(
				f"Private network CIDR {network} must have a prefix length between "
				f"/{self.provider.private_network_min_prefix} and /{self.provider.private_network_max_prefix}."
			)

	def validate_credentials(self) -> bool:
		"""Return true when the configured keys can access AWS."""
		self.provider.client.call("sts", "get_caller_identity")
		return True

	def create_vpc(self) -> str:
		"""Return the Atlas VPC ID, or create an Atlas VPC."""
		cidr = self.private_network_cidr
		existing = self.find_configured_or_named(
			"describe_vpcs",
			"Vpcs",
			"VpcIds",
			self.provider.configuration.vpc_id,
			self.resource_name("VPC"),
		)
		if existing is not None:
			if existing["CidrBlock"] != cidr:
				raise AwsError(f"VPC {existing['VpcId']} does not use CIDR {cidr}")
			vpc_id = existing["VpcId"]
		else:
			response = self.provider.client.call(
				"ec2",
				"create_vpc",
				CidrBlock=cidr,
				TagSpecifications=[self.tag_specification("vpc", "VPC")],
			)
			vpc_id = response["Vpc"]["VpcId"]
		self.provider.client.call(
			"ec2", "modify_vpc_attribute", VpcId=vpc_id, EnableDnsHostnames={"Value": True}
		)
		if not self.vpc_ipv6_cidr(vpc_id):
			self.provider.client.call(
				"ec2", "associate_vpc_cidr_block", VpcId=vpc_id, AmazonProvidedIpv6CidrBlock=True
			)
			self.provider.poll(
				lambda: bool(self.vpc_ipv6_cidr(vpc_id)) or None,
				timeout_seconds=self.provider.setup_poll_timeout_seconds,
				poll_interval_seconds=self.provider.setup_poll_interval_seconds,
				description=f"the IPv6 block of VPC {vpc_id}",
			)
		return vpc_id

	def create_internet_access(self, vpc_id: str) -> None:
		"""Give the Atlas VPC a default route to the internet."""
		gateways = self.provider.client.call(
			"ec2",
			"describe_internet_gateways",
			Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}],
		).get("InternetGateways", [])
		if gateways:
			gateway_id = gateways[0]["InternetGatewayId"]
		else:
			response = self.provider.client.call(
				"ec2",
				"create_internet_gateway",
				TagSpecifications=[self.tag_specification("internet-gateway", "internet gateway")],
			)
			gateway_id = response["InternetGateway"]["InternetGatewayId"]
			self.provider.client.call(
				"ec2", "attach_internet_gateway", InternetGatewayId=gateway_id, VpcId=vpc_id
			)

		route_table_id, default_gateway_id = self.main_route_table(vpc_id)
		if default_gateway_id and default_gateway_id != gateway_id:
			raise AwsError(f"VPC {vpc_id} has a default route through {default_gateway_id}")
		if not default_gateway_id:
			self.provider.client.call(
				"ec2",
				"create_route",
				RouteTableId=route_table_id,
				DestinationCidrBlock="0.0.0.0/0",
				GatewayId=gateway_id,
			)
		if not self.has_ipv6_default_route(route_table_id):
			self.provider.client.call(
				"ec2",
				"create_route",
				RouteTableId=route_table_id,
				DestinationIpv6CidrBlock="::/0",
				GatewayId=gateway_id,
			)

	def create_subnet(self, vpc_id: str) -> str:
		"""Return the Atlas subnet ID, or create the single Atlas subnet.

		Atlas uses one subnet for the whole region. A public IPv6 block is a /80 of
		that subnet, so it can move to any host.
		"""
		existing = self.find_configured_or_named(
			"describe_subnets",
			"Subnets",
			"SubnetIds",
			self.provider.configuration.subnet_id,
			self.resource_name("subnet"),
		)
		if existing is not None:
			if existing["VpcId"] != vpc_id:
				raise AwsError(f"Subnet {existing['SubnetId']} does not belong to VPC {vpc_id}")
			if existing.get("CidrBlock") != self.private_network_cidr:
				raise AwsError(f"Subnet {existing['SubnetId']} does not use the Atlas private network")
			if existing.get("AvailabilityZone") != self.provider.configuration.availability_zone:
				raise AwsError(f"Subnet {existing['SubnetId']} is in another Availability Zone")
			subnet_id = existing["SubnetId"]
		else:
			response = self.provider.client.call(
				"ec2",
				"create_subnet",
				VpcId=vpc_id,
				CidrBlock=self.private_network_cidr,
				AvailabilityZone=self.provider.configuration.availability_zone,
				TagSpecifications=[self.tag_specification("subnet", "subnet")],
			)
			subnet_id = response["Subnet"]["SubnetId"]
		self.provider.client.call(
			"ec2",
			"modify_subnet_attribute",
			SubnetId=subnet_id,
			MapPublicIpOnLaunch={"Value": True},
		)
		if not (existing or {}).get("Ipv6CidrBlockAssociationSet"):
			vpc_block = ipaddress.IPv6Network(self.vpc_ipv6_cidr(vpc_id) or "")
			self.provider.client.call(
				"ec2",
				"associate_subnet_cidr_block",
				SubnetId=subnet_id,
				Ipv6CidrBlock=str(next(vpc_block.subnets(new_prefix=64))),
			)
		return subnet_id

	def create_security_group(self, vpc_id: str) -> str:
		"""Return the Atlas security group ID, or create the group."""
		existing = self.find_configured_or_named(
			"describe_security_groups",
			"SecurityGroups",
			"GroupIds",
			self.provider.configuration.security_group_id,
			self.resource_name("security group"),
		)
		if existing is not None:
			if existing.get("VpcId") != vpc_id:
				raise AwsError(f"Security group {existing['GroupId']} does not belong to VPC {vpc_id}")
			group_id = existing["GroupId"]
			permissions = existing.get("IpPermissions", [])
		else:
			response = self.provider.client.call(
				"ec2",
				"create_security_group",
				GroupName=self.resource_name("security group"),
				Description=self.resource_name("hosts"),
				VpcId=vpc_id,
				TagSpecifications=[self.tag_specification("security-group", "security group")],
			)
			group_id = response["GroupId"]
			permissions = []

		desired_rules = self.ingress_rules
		missing_rules = [rule for rule in desired_rules if not self.has_ingress_rule(permissions, rule)]
		if missing_rules:
			self.provider.client.call(
				"ec2", "authorize_security_group_ingress", GroupId=group_id, IpPermissions=missing_rules
			)

		obsolete_rules = [
			permission
			for permission in permissions
			if not any(self.has_ingress_rule([permission], rule) for rule in desired_rules)
		]
		if obsolete_rules:
			self.provider.client.call(
				"ec2", "revoke_security_group_ingress", GroupId=group_id, IpPermissions=obsolete_rules
			)
		return group_id

	def create_key_pair(self, public_key: str) -> str:
		"""Return the Atlas key pair name, or import the Atlas public key."""
		name = self.provider.configuration.key_pair_name or self.resource_name("SSH key")
		existing = self.provider.client.call("ec2", "describe_key_pairs", KeyNames=[name], allow_missing=True)
		if existing.get("KeyPairs"):
			return name

		self.provider.client.call(
			"ec2",
			"import_key_pair",
			KeyName=name,
			PublicKeyMaterial=public_key.encode(),
			TagSpecifications=[self.tag_specification("key-pair", "SSH key")],
		)
		return name

	def find(
		self,
		service: str,
		operation: str,
		key: str,
		name: str,
		*,
		ignored_states: tuple[str, ...] = (),
	) -> dict | None:
		"""Return the single AWS resource that carries one Atlas name tag."""
		response = self.provider.client.call(
			service, operation, Filters=[{"Name": "tag:Name", "Values": [name]}]
		)
		items = [item for item in response.get(key, []) if item.get("State") not in ignored_states]
		if len(items) > 1:
			raise AwsError(f"AWS returned multiple resources named {name}")
		return items[0] if items else None

	def find_configured_or_named(
		self,
		operation: str,
		key: str,
		ids_parameter: str,
		configured_id: str | None,
		name: str,
		*,
		ignored_states: tuple[str, ...] = (),
	) -> dict | None:
		"""Return a configured AWS resource, or find it by its Atlas name."""
		if not configured_id:
			return self.find("ec2", operation, key, name, ignored_states=ignored_states)

		response = self.provider.client.call("ec2", operation, **{ids_parameter: [configured_id]})
		items = [item for item in response.get(key, []) if item.get("State") not in ignored_states]
		if len(items) != 1:
			raise AwsError(f"AWS did not return configured resource {configured_id}")
		return items[0]

	def main_route_table(self, vpc_id: str) -> tuple[str, str | None]:
		"""Return the main route table and the gateway for its default route."""
		response = self.provider.client.call(
			"ec2",
			"describe_route_tables",
			Filters=[
				{"Name": "vpc-id", "Values": [vpc_id]},
				{"Name": "association.main", "Values": ["true"]},
			],
		)
		tables = response.get("RouteTables", [])
		if not tables:
			raise AwsError(f"VPC {vpc_id} has no main route table")
		default_route = next(
			(
				route
				for route in tables[0].get("Routes", [])
				if route.get("DestinationCidrBlock") == "0.0.0.0/0"
			),
			None,
		)
		return tables[0]["RouteTableId"], default_route.get("GatewayId") if default_route else None

	def vpc_ipv6_cidr(self, vpc_id: str) -> str | None:
		"""Return the associated Amazon IPv6 /56 of the VPC. VM public IPv6 blocks come from it."""
		vpcs = self.provider.client.call("ec2", "describe_vpcs", VpcIds=[vpc_id]).get("Vpcs", [])
		for association in vpcs[0].get("Ipv6CidrBlockAssociationSet", []) if vpcs else []:
			if association.get("Ipv6CidrBlockState", {}).get("State") == "associated":
				return association.get("Ipv6CidrBlock")
		return None

	def has_ipv6_default_route(self, route_table_id: str) -> bool:
		"""Report whether the route table sends IPv6 to a gateway."""
		tables = self.provider.client.call(
			"ec2", "describe_route_tables", RouteTableIds=[route_table_id]
		).get("RouteTables", [])
		return any(
			route.get("DestinationIpv6CidrBlock") == "::/0"
			for table in tables
			for route in table.get("Routes", [])
		)

	@property
	def private_network_cidr(self) -> str:
		"""Return the canonical Atlas private network CIDR."""
		return str(ipaddress.ip_network(self.provider.settings.private_network_cidr, strict=False))

	@staticmethod
	def has_ingress_rule(permissions: object, expected: dict) -> bool:
		"""Report whether AWS returned one required ingress rule."""
		if not isinstance(permissions, list):
			return False
		expected_cidrs = {item["CidrIp"] for item in expected.get("IpRanges", [])} | {
			item["CidrIpv6"] for item in expected.get("Ipv6Ranges", [])
		}
		for permission in permissions:
			if not isinstance(permission, dict):
				continue
			actual_cidrs = {item.get("CidrIp") for item in permission.get("IpRanges", [])} | {
				item.get("CidrIpv6") for item in permission.get("Ipv6Ranges", [])
			}
			if (
				permission.get("IpProtocol") == expected.get("IpProtocol")
				and permission.get("FromPort") == expected.get("FromPort")
				and permission.get("ToPort") == expected.get("ToPort")
				and expected_cidrs <= actual_cidrs
			):
				return True
		return False

	def resource_name(self, detail: str) -> str:
		"""Return the Atlas name for one shared AWS resource."""
		return self.provider.configuration.resource_name(detail)

	def tag_specification(self, resource_type: str, kind: str) -> dict:
		"""Return the Atlas name tag for one new AWS resource."""
		return {
			"ResourceType": resource_type,
			"Tags": [{"Key": "Name", "Value": self.resource_name(kind)}],
		}

	@property
	def ingress_rules(self) -> list[dict]:
		"""Allow all inbound traffic. The host firewall and Metal filter it."""
		return [
			{
				"IpProtocol": "-1",
				"IpRanges": [
					{"CidrIp": "0.0.0.0/0", "Description": self.resource_name("all inbound traffic")}
				],
			},
			{
				"IpProtocol": "-1",
				"Ipv6Ranges": [
					{"CidrIpv6": "::/0", "Description": self.resource_name("all inbound traffic")}
				],
			},
		]
