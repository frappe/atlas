from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.aws.catalog import AwsCatalog
from atlas.atlas.core.server_providers.aws.client import AwsError
from atlas.atlas.core.server_providers.aws.configuration import (
	ROOT_VOLUME_SIZE_GIB,
	STORAGE_VOLUME_DEVICE_NAME,
	STORAGE_VOLUME_SIZE_GIB,
	AwsConfiguration,
)
from atlas.atlas.core.server_providers.aws.infrastructure import AwsInfrastructure
from atlas.atlas.core.server_providers.aws.ip_addresses import AwsIPAddresses, AwsIPv6Prefixes
from atlas.atlas.core.server_providers.aws.provider import AwsProvider
from atlas.atlas.core.server_providers.aws.servers import AwsServers
from atlas.atlas.core.server_providers.aws.volumes import AwsVolumes
from atlas.atlas.core.server_providers.base import ProviderServer, ServerCreateRequest, ServerPowerAction
from atlas.atlas.core.server_providers.registry import get_server_provider


def aws_configuration(**changes: object) -> AwsConfiguration:
	values = {
		"region": "eu-west-1",
		"availability_zone": "eu-west-1a",
		"region_name": "eu",
		"vpc_id": None,
		"subnet_id": None,
		"security_group_id": None,
		"key_pair_name": None,
	}
	values.update(changes)
	return AwsConfiguration(**values)


class TestAwsInfrastructure(UnitTestCase):
	def test_settings_need_a_private_ipv4_network(self) -> None:
		infrastructure = self.infrastructure(private_network_cidr="8.8.0.0/16")

		with self.assertRaisesRegex(AwsError, "private IPv4"):
			infrastructure.validate_settings()

	def test_settings_need_unicast_networking(self) -> None:
		infrastructure = self.infrastructure(is_unicast_network_enabled=0)

		with self.assertRaisesRegex(AwsError, "unicast networking"):
			infrastructure.validate_settings()

	def test_existing_vpc_is_reconciled_by_its_stored_id(self) -> None:
		infrastructure = self.infrastructure(vpc_id="vpc-1", private_network_cidr="10.1.1.1/20")
		infrastructure.provider.client.call.return_value = {
			"Vpcs": [
				{
					"VpcId": "vpc-1",
					"CidrBlock": "10.1.0.0/20",
					"Ipv6CidrBlockAssociationSet": [
						{"Ipv6CidrBlock": "2600:1f18::/56", "Ipv6CidrBlockState": {"State": "associated"}}
					],
				}
			]
		}

		vpc_id = infrastructure.create_vpc()

		self.assertEqual(vpc_id, "vpc-1")
		first_call = infrastructure.provider.client.call.call_args_list[0]
		self.assertEqual(first_call.kwargs["VpcIds"], ["vpc-1"])
		self.assertEqual(
			infrastructure.provider.client.call.call_args_list[1].args[1], "modify_vpc_attribute"
		)

	def test_security_group_replaces_old_ingress_rules(self) -> None:
		infrastructure = self.infrastructure(security_group_id="sg-1")
		old_rule = {
			"IpProtocol": "tcp",
			"FromPort": 22,
			"ToPort": 22,
			"IpRanges": [{"CidrIp": "203.0.113.0/24"}],
		}
		infrastructure.find_configured_or_named = Mock(
			return_value={"GroupId": "sg-1", "VpcId": "vpc-1", "IpPermissions": [old_rule]}
		)

		self.assertEqual(infrastructure.create_security_group("vpc-1"), "sg-1")

		operations = [call.args[1] for call in infrastructure.provider.client.call.call_args_list]
		self.assertEqual(operations, ["authorize_security_group_ingress", "revoke_security_group_ingress"])
		self.assertEqual(
			infrastructure.provider.client.call.call_args_list[1].kwargs["IpPermissions"], [old_rule]
		)

	def test_the_internet_gateway_is_found_by_its_vpc(self) -> None:
		infrastructure = self.infrastructure()
		infrastructure.main_route_table = Mock(return_value=("rtb-1", "igw-1"))
		infrastructure.has_ipv6_default_route = Mock(return_value=True)
		infrastructure.provider.client.call.return_value = {
			"InternetGateways": [{"InternetGatewayId": "igw-1"}]
		}

		infrastructure.create_internet_access("vpc-1")

		self.assertEqual(
			infrastructure.provider.client.call.call_args.kwargs["Filters"],
			[{"Name": "attachment.vpc-id", "Values": ["vpc-1"]}],
		)
		operations = [call.args[1] for call in infrastructure.provider.client.call.call_args_list]
		self.assertNotIn("create_internet_gateway", operations)

	def test_the_stored_key_pair_name_is_kept(self) -> None:
		infrastructure = self.infrastructure(key_pair_name="atlas-eu-ssh-key")
		infrastructure.provider.client.call.return_value = {"KeyPairs": [{"KeyName": "atlas-eu-ssh-key"}]}

		self.assertEqual(infrastructure.create_key_pair("ssh-ed25519 key"), "atlas-eu-ssh-key")

	@staticmethod
	def infrastructure(**changes: object) -> AwsInfrastructure:
		configuration = {}
		settings = {"private_network_cidr": "10.1.0.0/20", "is_unicast_network_enabled": 1}
		for key, value in changes.items():
			if key in settings:
				settings[key] = value
			else:
				configuration[key] = value
		provider = SimpleNamespace(
			configuration=aws_configuration(**configuration),
			settings=SimpleNamespace(**settings),
			private_network_min_prefix=16,
			private_network_max_prefix=28,
			client=Mock(),
		)
		return AwsInfrastructure(provider)


class TestAwsProvider(UnitTestCase):
	def test_the_registry_resolves_the_aws_provider(self) -> None:
		with patch.object(AwsProvider, "__init__", return_value=None):
			self.assertIsInstance(get_server_provider("AWS"), AwsProvider)

	def test_setup_infrastructure_saves_each_named_resource(self) -> None:
		provider = self.provider()
		provider.settings.db_set = Mock()
		provider.settings.save = Mock()
		provider.infrastructure = Mock()
		provider.infrastructure.create_vpc.return_value = "vpc-1"
		provider.infrastructure.create_subnet.return_value = "subnet-1"
		provider.infrastructure.create_security_group.return_value = "sg-1"
		provider.infrastructure.create_key_pair.return_value = "atlas-eu-ssh-key"

		provider.setup_infrastructure()

		self.assertEqual(
			[field_call.args[:2] for field_call in provider.settings.db_set.call_args_list],
			[
				("aws_vpc_id", "vpc-1"),
				("aws_subnet_id", "subnet-1"),
				("aws_security_group_id", "sg-1"),
				("aws_key_pair_name", "atlas-eu-ssh-key"),
			],
		)
		self.assertEqual(provider.settings.is_server_provider_setup_completed, 1)
		provider.settings.save.assert_called_once_with()

	def test_prepare_server_attaches_the_host_address_and_reads_the_private_address(self) -> None:
		provider = self.provider()
		order = Mock()
		provider.wait_for_server_ready = Mock(side_effect=lambda _server: order("ready"))
		provider.attach_host_address = Mock(side_effect=lambda _server: order("host address"))
		server = self.server("i-1")
		server.provider_metadata = json.dumps(
			{
				"instance": {
					"NetworkInterfaces": [
						{
							"NetworkInterfaceId": "eni-1",
							"PrivateIpAddress": "10.1.3.95",
							"Attachment": {"DeviceIndex": 0},
						}
					]
				}
			}
		)

		provider.prepare_server(server)

		self.assertEqual([call.args[0] for call in order.call_args_list], ["ready", "host address"])
		self.assertEqual(server.private_ipv4_address, "10.1.3.95")

	def test_delete_server_releases_the_stored_host_address(self) -> None:
		provider = self.provider()

		provider.delete_server("i-1", {"host_address_allocation_id": "eipalloc-1"})

		provider.ip_addresses.release_host_address.assert_called_once_with("eipalloc-1")
		provider.servers.delete.assert_called_once_with("i-1")

	def test_delete_server_without_a_stored_host_address_releases_nothing(self) -> None:
		provider = self.provider()

		provider.delete_server("i-1", {})

		provider.ip_addresses.release_host_address.assert_not_called()
		provider.servers.delete.assert_called_once_with("i-1")

	def test_attach_host_address_keeps_the_allocation_when_the_association_fails(self) -> None:
		provider = self.provider()
		provider.ip_addresses.ensure_host_address.return_value = "eipalloc-1"
		provider.ip_addresses.associate_host_address.side_effect = AwsError("association failed")
		server = self.server("i-1")
		server.provider_metadata = json.dumps(
			{
				"instance": {
					"NetworkInterfaces": [{"NetworkInterfaceId": "eni-1", "Attachment": {"DeviceIndex": 0}}]
				}
			}
		)

		with self.assertRaisesRegex(AwsError, "association failed"):
			provider.attach_host_address(server)

		provider.ip_addresses.ensure_host_address.assert_called_once_with("server-1")
		self.assertEqual(json.loads(server.provider_metadata)["host_address_allocation_id"], "eipalloc-1")

	def test_configure_server_network_uses_the_primary_interface_for_the_mesh(self) -> None:
		provider = self.provider()
		server = self.server(provider_server_id="i-1")
		provider.uplink_interface = Mock(return_value="ens5")
		provider.wait_for_private_address = Mock()

		provider.configure_server_network(server)

		self.assertEqual(
			(server.public_network_interface, server.private_network_interface), ("ens5", "ens5")
		)
		provider.wait_for_private_address.assert_called_once_with(server)

	def test_a_public_address_attaches_to_the_primary_interface(self) -> None:
		provider = self.provider()
		provider.ip_addresses = Mock()
		server = self.server()
		server.provider_metadata = json.dumps(
			{
				"instance": {
					"NetworkInterfaces": [
						{"NetworkInterfaceId": "eni-other", "Attachment": {"DeviceIndex": 1}},
						{"NetworkInterfaceId": "eni-primary", "Attachment": {"DeviceIndex": 0}},
					]
				}
			}
		)

		provider.attach_public_ip_address("eipalloc-1", "203.0.113.9", server)

		provider.ip_addresses.attach.assert_called_once_with("eipalloc-1", "eni-primary")

	def test_a_public_address_needs_the_primary_interface(self) -> None:
		provider = self.provider()
		server = self.server()
		server.provider_metadata = json.dumps(
			{
				"instance": {
					"NetworkInterfaces": [
						{"NetworkInterfaceId": "eni-other", "Attachment": {"DeviceIndex": 1}}
					]
				}
			}
		)

		with self.assertRaisesRegex(AwsError, "primary network interface"):
			provider.attach_public_ip_address("eipalloc-1", "203.0.113.9", server)

	def test_the_storage_pool_device_names_the_attached_volume(self) -> None:
		provider = self.provider()
		server = self.server()
		server.provider_metadata = frappe.as_json(
			{
				"instance": {
					"BlockDeviceMappings": [
						{"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-0c0bcb91d7b5a82eb"}},
						{"DeviceName": "/dev/sdb", "Ebs": {"VolumeId": "vol-012ac512cc4f05420"}},
					]
				}
			}
		)

		self.assertEqual(
			provider.storage_pool_device(server),
			"/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol012ac512cc4f05420",
		)

	def test_a_server_without_a_storage_volume_is_refused(self) -> None:
		provider = self.provider()

		with self.assertRaises(AwsError):
			provider.storage_pool_device(self.server())

	def test_the_security_group_allows_all_inbound_traffic(self) -> None:
		rules = AwsInfrastructure(self.provider()).ingress_rules

		self.assertEqual(
			[
				(
					rule["IpProtocol"],
					[
						r.get("CidrIp") or r.get("CidrIpv6")
						for r in rule.get("IpRanges", []) + rule.get("Ipv6Ranges", [])
					],
				)
				for rule in rules
			],
			[("-1", ["0.0.0.0/0"]), ("-1", ["::/0"])],
		)

	def test_ubuntu_and_debian_users_can_be_promoted(self) -> None:
		provider = self.provider()
		provider.run_setup_script = Mock()

		provider.promote_ssh_user(self.server(), "admin")

		provider.run_setup_script.assert_called_once_with(
			self.server(), "promote-ssh-user.sh", ssh_user="admin"
		)

	def test_an_unknown_ssh_user_cannot_be_promoted(self) -> None:
		provider = self.provider()
		provider.run_setup_script = Mock()

		with self.assertRaises(AwsError):
			provider.promote_ssh_user(self.server(), "ec2-user")

	def test_uplink_interface_reads_the_default_route_device(self) -> None:
		provider = self.provider()
		runner = Mock()
		runner.run_command.return_value = SimpleNamespace(
			exit_code=0, output="default via 10.1.0.1 dev ens5 proto dhcp"
		)

		with patch("atlas.atlas.core.ssh.SSHRunner", return_value=runner):
			self.assertEqual(provider.uplink_interface(self.server()), "ens5")

	def test_uplink_interface_fails_without_a_default_route(self) -> None:
		provider = self.provider()
		runner = Mock()
		runner.run_command.return_value = SimpleNamespace(exit_code=0, output="")

		with patch("atlas.atlas.core.ssh.SSHRunner", return_value=runner), self.assertRaises(AwsError):
			provider.uplink_interface(self.server())

	def test_an_import_outside_the_atlas_subnet_is_refused(self) -> None:
		"""Public addresses attach to the primary interface, so it must be in the Atlas subnet."""
		provider = self.provider()
		provider.configuration = SimpleNamespace(subnet_id="subnet-atlas")
		provider.servers.fetch.return_value = {"ImageId": "ami-1", "SubnetId": "subnet-other"}

		with self.assertRaisesRegex(AwsError, "not the Atlas subnet subnet-atlas"):
			provider.import_server(self.server("i-1"))

	def test_an_import_matches_an_older_image_of_a_catalog_version(self) -> None:
		"""The catalog keeps only the newest image, and an instance can run an older one."""
		provider = self.provider()
		provider.configuration = SimpleNamespace(subnet_id="subnet-atlas")
		provider.servers.fetch.return_value = {
			"ImageId": "ami-old",
			"SubnetId": "subnet-atlas",
			"InstanceType": "c7i.xlarge",
		}
		provider.client.call.return_value = {
			"Images": [
				{
					"ImageId": "ami-old",
					"Name": "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-20260904",
					"CreationDate": "2026-09-04T11:45:55.000Z",
				}
			]
		}
		provider.apply_provider_server = Mock()
		server = self.server("i-1")

		with patch("atlas.atlas.core.server_providers.aws.provider.frappe.db.exists", return_value=True):
			provider.import_server(server)

		self.assertEqual(server.server_image, "Ubuntu_24.04")

	def provider(self) -> AwsProvider:
		provider = object.__new__(AwsProvider)
		provider.settings = SimpleNamespace(
			private_network_cidr="10.1.0.0/20",
			private_network_mtu=9001,
			public_ssh_key="ssh-ed25519 key",
			is_server_provider_setup_completed=0,
		)
		provider.configuration = aws_configuration()
		provider.client = Mock()
		provider.catalog = AwsCatalog()
		provider.infrastructure = Mock()
		provider.servers = Mock()
		provider.ip_addresses = Mock()
		provider.volumes = AwsVolumes(provider)
		return provider

	@staticmethod
	def server(provider_server_id: str | None = None) -> SimpleNamespace:
		return SimpleNamespace(
			doctype="Metal Server",
			name="server-1",
			provider_server_id=provider_server_id,
			provider_metadata="{}",
			public_ipv4_address="203.0.113.1",
			ssh_host="203.0.113.1",
			private_ipv4_address=None,
			public_network_interface=None,
			private_network_interface=None,
			status="Installing",
		)


class TestAwsServers(UnitTestCase):
	def test_create_needs_every_stored_infrastructure_identity(self) -> None:
		servers = self.servers()
		servers.configuration = SimpleNamespace(
			key_pair_name="atlas-eu-ssh-key",
			subnet_id="subnet-1",
			security_group_id=None,
		)

		with self.assertRaisesRegex(AwsError, "security group"):
			servers.create(self.request())

	def test_ensure_reuses_the_tagged_instance_on_a_retry(self) -> None:
		servers = self.servers()
		servers.client.paginate.return_value = [{"Instances": [self.instance()]}]
		servers.create = Mock()

		result = servers.ensure(self.request())

		self.assertEqual(result.provider_server_id, "i-1")
		servers.create.assert_not_called()

	def test_ensure_creates_an_instance_when_none_is_tagged(self) -> None:
		servers = self.servers()
		servers.client.paginate.return_value = []
		servers.client.call.return_value = {"Instances": [self.instance()]}

		result = servers.ensure(self.request())

		self.assertEqual(result.provider_server_id, "i-1")
		self.assertEqual(servers.client.call.call_args.args[1], "run_instances")
		self.assertEqual(servers.client.call.call_args.kwargs["ImageId"], "ami-1")
		self.assertEqual(
			servers.client.call.call_args.kwargs["ClientToken"],
			AwsServers.client_token("instance", "server-1"),
		)

	def test_create_resizes_the_image_root_device(self) -> None:
		servers = self.servers()
		servers.client.paginate.return_value = []
		servers.client.call.return_value = {"Instances": [self.instance()]}

		servers.ensure(self.request())

		self.assertEqual(
			servers.client.call.call_args.kwargs["BlockDeviceMappings"][0],
			{
				"DeviceName": "/dev/sda1",
				"Ebs": {
					"VolumeSize": ROOT_VOLUME_SIZE_GIB,
					"VolumeType": "gp3",
					"DeleteOnTermination": True,
				},
			},
		)

	def test_create_attaches_a_block_store_volume_for_the_pool(self) -> None:
		servers = self.servers()
		servers.client.paginate.return_value = []
		servers.client.call.return_value = {"Instances": [self.instance()]}

		servers.ensure(self.request())

		mappings = servers.client.call.call_args.kwargs["BlockDeviceMappings"]
		self.assertEqual(
			[mapping["DeviceName"] for mapping in mappings],
			["/dev/sda1", STORAGE_VOLUME_DEVICE_NAME],
		)
		self.assertEqual(mappings[1]["Ebs"]["VolumeSize"], STORAGE_VOLUME_SIZE_GIB)
		self.assertTrue(mappings[1]["Ebs"]["DeleteOnTermination"])

	def test_create_needs_the_image_root_device_name(self) -> None:
		servers = self.servers()
		request = ServerCreateRequest(
			name="server-1",
			server_size="c6i.metal",
			server_image="Ubuntu_24.04",
			size_provider_metadata={"BareMetal": True},
			image_provider_metadata={"ImageId": "ami-1"},
		)

		with self.assertRaisesRegex(AwsError, "root device name"):
			servers.create(request)

	def test_a_virtual_instance_asks_for_nested_virtualization(self) -> None:
		servers = self.servers()
		servers.client.paginate.return_value = []
		servers.client.call.return_value = {"Instances": [self.instance()]}

		servers.ensure(self.request(size_provider_metadata={"BareMetal": False}))

		self.assertEqual(
			servers.client.call.call_args.kwargs["CpuOptions"], {"NestedVirtualization": "enabled"}
		)

	def test_a_bare_metal_instance_does_not_ask_for_nested_virtualization(self) -> None:
		servers = self.servers()
		servers.client.paginate.return_value = []
		servers.client.call.return_value = {"Instances": [self.instance()]}

		servers.ensure(self.request(size_provider_metadata={"BareMetal": True}))

		self.assertNotIn("CpuOptions", servers.client.call.call_args.kwargs)

	def test_two_tagged_instances_fail_loudly(self) -> None:
		servers = self.servers()
		servers.client.paginate.return_value = [{"Instances": [self.instance(), self.instance("i-2")]}]

		with self.assertRaises(AwsError):
			servers.ensure(self.request())

	def test_the_power_action_uses_the_explicit_operation(self) -> None:
		servers = self.servers()

		servers.set_power_state("i-1", ServerPowerAction.STOP)

		self.assertEqual(servers.client.call.call_args.args[1], "stop_instances")
		self.assertEqual(servers.client.call.call_args.kwargs["InstanceIds"], ["i-1"])

	@staticmethod
	def servers() -> AwsServers:
		return AwsServers(
			client=Mock(),
			configuration=aws_configuration(
				subnet_id="subnet-1", security_group_id="sg-1", key_pair_name="atlas-eu-ssh-key"
			),
			catalog=AwsCatalog(),
		)

	@staticmethod
	def request(size_provider_metadata: dict | None = None) -> ServerCreateRequest:
		return ServerCreateRequest(
			name="server-1",
			server_size="c6i.metal",
			server_image="Ubuntu_24.04",
			size_provider_metadata=size_provider_metadata or {"BareMetal": True},
			image_provider_metadata={"ImageId": "ami-1", "RootDeviceName": "/dev/sda1"},
		)

	@staticmethod
	def instance(instance_id: str = "i-1") -> dict:
		return {
			"InstanceId": instance_id,
			"State": {"Name": "running"},
			"PublicIpAddress": "203.0.113.1",
		}


class TestAwsIPAddresses(UnitTestCase):
	def test_reserve_returns_the_address_and_allocation(self) -> None:
		addresses = self.addresses()
		addresses.client.call.return_value = {"PublicIp": "203.0.113.5", "AllocationId": "eipalloc-1"}

		reserved = addresses.reserve()

		self.assertEqual(reserved.address, "203.0.113.5")
		self.assertEqual(reserved.provider_resource_id, "eipalloc-1")

	def test_reserve_fails_loudly_on_an_incomplete_response(self) -> None:
		addresses = self.addresses()
		addresses.client.call.return_value = {"PublicIp": "203.0.113.5"}

		with self.assertRaises(AwsError):
			addresses.reserve()

	def test_attach_gives_the_address_its_own_private_address(self) -> None:
		addresses = self.addresses()
		addresses.client.call.side_effect = [
			{"Addresses": []},
			{"AssignedPrivateIpAddresses": [{"PrivateIpAddress": "10.1.8.55"}]},
			{},
		]

		host_address = addresses.attach("eipalloc-1", "eni-1")

		self.assertEqual(host_address, "10.1.8.55")
		parameters = addresses.client.call.call_args.kwargs
		self.assertEqual(parameters["NetworkInterfaceId"], "eni-1")
		self.assertEqual(parameters["PrivateIpAddress"], "10.1.8.55")
		self.assertNotIn("InstanceId", parameters)
		self.assertTrue(parameters["AllowReassociation"])

	def test_attach_reuses_the_private_address_it_already_assigned(self) -> None:
		addresses = self.addresses()
		addresses.client.call.side_effect = [
			{"Addresses": [{"NetworkInterfaceId": "eni-1", "PrivateIpAddress": "10.1.8.55"}]},
			{
				"NetworkInterfaces": [
					{"PrivateIpAddresses": [{"PrivateIpAddress": "10.1.8.4", "Primary": True}]}
				]
			},
			{},
		]

		host_address = addresses.attach("eipalloc-1", "eni-1")

		self.assertEqual(host_address, "10.1.8.55")
		operations = [call.args[1] for call in addresses.client.call.call_args_list]
		self.assertEqual(
			operations, ["describe_addresses", "describe_network_interfaces", "associate_address"]
		)

	def test_attach_never_reuses_the_address_of_the_host(self) -> None:
		addresses = self.addresses()
		addresses.client.call.side_effect = [
			{"Addresses": [{"NetworkInterfaceId": "eni-1", "PrivateIpAddress": "10.1.8.4"}]},
			{
				"NetworkInterfaces": [
					{"PrivateIpAddresses": [{"PrivateIpAddress": "10.1.8.4", "Primary": True}]}
				]
			},
			{"AssignedPrivateIpAddresses": [{"PrivateIpAddress": "10.1.8.55"}]},
			{},
		]

		host_address = addresses.attach("eipalloc-1", "eni-1")

		self.assertEqual(host_address, "10.1.8.55")
		self.assertEqual(addresses.client.call.call_args.kwargs["PrivateIpAddress"], "10.1.8.55")

	def test_attach_failure_removes_the_new_host_address(self) -> None:
		addresses = self.addresses()
		addresses.client.call.side_effect = [
			{"Addresses": []},
			{"AssignedPrivateIpAddresses": [{"PrivateIpAddress": "10.1.8.55"}]},
			AwsError("attach failed"),
			{},
		]

		with self.assertRaisesRegex(AwsError, "attach failed"):
			addresses.attach("eipalloc-1", "eni-1")

		operations = [call.args[1] for call in addresses.client.call.call_args_list]
		self.assertEqual(
			operations,
			[
				"describe_addresses",
				"assign_private_ip_addresses",
				"associate_address",
				"unassign_private_ip_addresses",
			],
		)

	def test_detach_removes_the_private_address_it_assigned(self) -> None:
		addresses = self.addresses()
		addresses.client.call.side_effect = [
			{
				"Addresses": [
					{
						"AssociationId": "eipassoc-1",
						"NetworkInterfaceId": "eni-1",
						"PrivateIpAddress": "10.1.8.55",
					}
				]
			},
			{
				"NetworkInterfaces": [
					{"PrivateIpAddresses": [{"PrivateIpAddress": "10.1.8.4", "Primary": True}]}
				]
			},
			{},
			{},
		]

		addresses.detach("eipalloc-1", "eni-1", "10.1.8.55")

		operations = [call.args[1] for call in addresses.client.call.call_args_list]
		self.assertEqual(
			operations,
			[
				"describe_addresses",
				"describe_network_interfaces",
				"disassociate_address",
				"unassign_private_ip_addresses",
			],
		)

	def test_detach_recovers_a_missing_saved_host_address(self) -> None:
		addresses = self.addresses()
		addresses.client.call.side_effect = [
			{
				"Addresses": [
					{
						"AssociationId": "eipassoc-1",
						"NetworkInterfaceId": "eni-1",
						"PrivateIpAddress": "10.1.8.55",
					}
				]
			},
			{
				"NetworkInterfaces": [
					{"PrivateIpAddresses": [{"PrivateIpAddress": "10.1.8.4", "Primary": True}]}
				]
			},
			{},
			{},
		]

		addresses.detach("eipalloc-1", "eni-1", None)

		self.assertEqual(addresses.client.call.call_args.kwargs["PrivateIpAddresses"], ["10.1.8.55"])

	def test_detach_retry_removes_the_saved_host_address(self) -> None:
		addresses = self.addresses()
		addresses.client.call.side_effect = [
			{"Addresses": [{"AllocationId": "eipalloc-1"}]},
			{
				"NetworkInterfaces": [
					{"PrivateIpAddresses": [{"PrivateIpAddress": "10.1.8.4", "Primary": True}]}
				]
			},
			{},
		]

		addresses.detach("eipalloc-1", "eni-1", "10.1.8.55")

		operations = [call.args[1] for call in addresses.client.call.call_args_list]
		self.assertEqual(
			operations,
			["describe_addresses", "describe_network_interfaces", "unassign_private_ip_addresses"],
		)

	def test_detach_refuses_to_unassign_the_primary_address(self) -> None:
		addresses = self.addresses()
		addresses.client.call.return_value = {
			"NetworkInterfaces": [{"PrivateIpAddresses": [{"PrivateIpAddress": "10.1.8.4", "Primary": True}]}]
		}

		with self.assertRaisesRegex(AwsError, "Refusing to unassign primary"):
			addresses.detach("eipalloc-1", "eni-1", "10.1.8.4")

		operations = [call.args[1] for call in addresses.client.call.call_args_list]
		self.assertEqual(operations, ["describe_addresses", "describe_network_interfaces"])

	def test_host_address_retry_reuses_the_tagged_address(self) -> None:
		addresses = self.addresses()
		addresses.client.call.return_value = {"Addresses": [{"AllocationId": "eipalloc-host"}]}

		self.assertEqual(addresses.ensure_host_address("server-1"), "eipalloc-host")

		operations = [call.args[1] for call in addresses.client.call.call_args_list]
		self.assertEqual(operations, ["describe_addresses"])

	def test_host_address_is_allocated_with_the_server_tag(self) -> None:
		addresses = self.addresses()
		addresses.client.call.side_effect = [{"Addresses": []}, {"AllocationId": "eipalloc-host"}]

		self.assertEqual(addresses.ensure_host_address("server-1"), "eipalloc-host")

		tags = addresses.client.call.call_args.kwargs["TagSpecifications"][0]["Tags"]
		self.assertIn({"Key": AwsServers.identity_tag_key, "Value": "server-1"}, tags)

	def test_two_tagged_host_addresses_fail_loudly(self) -> None:
		addresses = self.addresses()
		addresses.client.call.return_value = {"Addresses": [{"AllocationId": "a"}, {"AllocationId": "b"}]}

		with self.assertRaisesRegex(AwsError, "multiple Elastic IP"):
			addresses.ensure_host_address("server-1")

	def test_host_address_goes_to_the_primary_private_address(self) -> None:
		addresses = self.addresses()
		addresses.client.call.side_effect = [
			{
				"NetworkInterfaces": [
					{"PrivateIpAddresses": [{"PrivateIpAddress": "10.1.3.95", "Primary": True}]}
				]
			},
			{"Addresses": [{"AllocationId": "eipalloc-host"}]},
			{},
		]

		addresses.associate_host_address("eipalloc-host", "eni-1")

		parameters = addresses.client.call.call_args.kwargs
		self.assertEqual(
			(parameters["NetworkInterfaceId"], parameters["PrivateIpAddress"]), ("eni-1", "10.1.3.95")
		)
		self.assertFalse(parameters["AllowReassociation"])

	def test_an_associated_host_address_is_left_alone(self) -> None:
		addresses = self.addresses()
		addresses.client.call.side_effect = [
			{
				"NetworkInterfaces": [
					{"PrivateIpAddresses": [{"PrivateIpAddress": "10.1.3.95", "Primary": True}]}
				]
			},
			{"Addresses": [{"NetworkInterfaceId": "eni-1", "PrivateIpAddress": "10.1.3.95"}]},
		]

		addresses.associate_host_address("eipalloc-host", "eni-1")

		operations = [call.args[1] for call in addresses.client.call.call_args_list]
		self.assertNotIn("associate_address", operations)

	def test_release_disassociates_the_host_address_first(self) -> None:
		addresses = self.addresses()
		addresses.client.call.side_effect = [{"Addresses": [{"AssociationId": "eipassoc-1"}]}, {}, {}]

		addresses.release_host_address("eipalloc-host")

		operations = [call.args[1] for call in addresses.client.call.call_args_list]
		self.assertEqual(operations, ["describe_addresses", "disassociate_address", "release_address"])

	def test_delete_is_idempotent(self) -> None:
		addresses = self.addresses()

		addresses.delete("eipalloc-1")

		self.assertTrue(addresses.client.call.call_args.kwargs["allow_missing"])

	@staticmethod
	def addresses() -> AwsIPAddresses:
		return AwsIPAddresses(client=Mock(), configuration=aws_configuration())

	def test_an_ipv6_block_is_the_first_free_80_of_the_subnet(self) -> None:
		client = Mock()
		client.call.return_value = {
			"Subnets": [
				{
					"Ipv6CidrBlockAssociationSet": [
						{"Ipv6CidrBlock": "2600:1f18:0:1::/64", "Ipv6CidrBlockState": {"State": "associated"}}
					]
				}
			]
		}
		prefixes = AwsIPv6Prefixes(client, SimpleNamespace(subnet_id="subnet-1"))

		reserved = prefixes.reserve({"2600:1f18:0:1:1::/80"})

		# The first /80 holds the addresses that AWS reserves in every subnet.
		self.assertEqual(reserved.address, "2600:1f18:0:1:2::/80")
		self.assertEqual(reserved.provider_resource_id, "2600:1f18:0:1:2::/80")

	def test_an_ipv6_block_is_delegated_to_the_host_interface(self) -> None:
		client = Mock()
		prefixes = AwsIPv6Prefixes(client, SimpleNamespace(subnet_id="subnet-1"))

		prefixes.attach("2600:1f18:0:1:1::/80", "eni-1")

		client.call.assert_called_once_with(
			"ec2", "assign_ipv6_addresses", NetworkInterfaceId="eni-1", Ipv6Prefixes=["2600:1f18:0:1:1::/80"]
		)
