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
)
from atlas.atlas.core.server_providers.aws.infrastructure import AwsInfrastructure
from atlas.atlas.core.server_providers.aws.ip_addresses import AwsIPAddresses
from atlas.atlas.core.server_providers.aws.provider import AwsProvider
from atlas.atlas.core.server_providers.aws.servers import MESH_MULTICAST_GROUP, AwsServers
from atlas.atlas.core.server_providers.aws.volumes import AwsVolumes
from atlas.atlas.core.server_providers.base import ProviderServer, ServerCreateRequest, ServerPowerAction
from atlas.atlas.core.server_providers.registry import get_server_provider


class TestAwsInfrastructure(UnitTestCase):
	def test_settings_need_a_private_ipv4_network(self) -> None:
		infrastructure = self.infrastructure(private_network_cidr="8.8.0.0/16")

		with self.assertRaisesRegex(AwsError, "private IPv4"):
			infrastructure.validate_settings()

	def test_existing_vpc_is_reconciled_by_its_stored_id(self) -> None:
		infrastructure = self.infrastructure(vpc_id="vpc-1", private_network_cidr="10.1.1.1/20")
		infrastructure.provider.client.call.return_value = {
			"Vpcs": [{"VpcId": "vpc-1", "CidrBlock": "10.1.0.0/20"}]
		}

		vpc_id = infrastructure.create_vpc()

		self.assertEqual(vpc_id, "vpc-1")
		first_call = infrastructure.provider.client.call.call_args_list[0]
		self.assertEqual(first_call.kwargs["VpcIds"], ["vpc-1"])
		self.assertEqual(
			infrastructure.provider.client.call.call_args_list[1].args[1], "modify_vpc_attribute"
		)

	def test_existing_attachment_must_use_the_atlas_network(self) -> None:
		infrastructure = self.infrastructure(transit_gateway_attachment_id="tgw-attach-1")
		infrastructure.provider.client.call.return_value = {
			"TransitGatewayVpcAttachments": [
				{
					"TransitGatewayAttachmentId": "tgw-attach-1",
					"TransitGatewayId": "tgw-1",
					"VpcId": "vpc-2",
					"SubnetIds": ["subnet-2"],
				}
			]
		}

		with self.assertRaisesRegex(AwsError, "does not use the Atlas VPC"):
			infrastructure.attach_transit_gateway("tgw-1", "vpc-1", "subnet-1")

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

	@staticmethod
	def infrastructure(**changes: object) -> AwsInfrastructure:
		configuration = {
			"region": "eu-west-1",
			"availability_zone": "eu-west-1a",
			"resource_name_prefix": "atlas-eu-",
			"vpc_id": None,
			"subnet_id": None,
			"security_group_id": None,
			"key_pair_name": None,
			"transit_gateway_id": None,
			"transit_gateway_attachment_id": None,
			"multicast_domain_id": None,
		}
		settings = {"private_network_cidr": "10.1.0.0/20"}
		for key, value in changes.items():
			if key == "private_network_cidr":
				settings[key] = value
			else:
				configuration[key] = value
		provider = SimpleNamespace(
			configuration=SimpleNamespace(**configuration),
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
		provider.infrastructure.create_transit_gateway.return_value = "tgw-1"
		provider.infrastructure.attach_transit_gateway.return_value = "tgw-attach-1"
		provider.infrastructure.create_multicast_domain.return_value = "tgw-mcast-1"

		provider.setup_infrastructure()

		self.assertEqual(
			[field_call.args[:2] for field_call in provider.settings.db_set.call_args_list],
			[
				("aws_vpc_id", "vpc-1"),
				("aws_subnet_id", "subnet-1"),
				("aws_security_group_id", "sg-1"),
				("aws_key_pair_name", "atlas-eu-ssh-key"),
				("aws_transit_gateway_id", "tgw-1"),
				("aws_transit_gateway_attachment_id", "tgw-attach-1"),
				("aws_multicast_domain_id", "tgw-mcast-1"),
			],
		)
		self.assertEqual(provider.settings.is_server_provider_setup_completed, 1)
		provider.settings.save.assert_called_once_with()

	def test_setup_waits_for_the_transit_gateway_before_it_attaches_the_vpc(self) -> None:
		provider = self.provider()
		provider.settings.db_set = Mock()
		order = Mock()
		provider.infrastructure = Mock()
		provider.infrastructure.create_transit_gateway.side_effect = lambda: order("create") or "tgw-1"
		provider.infrastructure.wait_for_available.side_effect = lambda *arguments, **_: order(arguments[1])
		provider.infrastructure.attach_transit_gateway.side_effect = lambda *_: (
			order("attach") or "tgw-attach-1"
		)
		provider.infrastructure.create_multicast_domain.side_effect = lambda _id: order("domain") or "d-1"
		provider.infrastructure.associate_multicast_subnet.side_effect = lambda *_: order("associate")

		provider.setup_multicast_domain("vpc-1", "subnet-1")

		self.assertEqual(
			[call.args[0] for call in order.call_args_list],
			[
				"create",
				"TransitGateways",
				"attach",
				"TransitGatewayVpcAttachments",
				"domain",
				"TransitGatewayMulticastDomains",
				"associate",
			],
		)

	def test_prepare_server_waits_before_it_attaches_the_mesh_interface(self) -> None:
		provider = self.provider()
		order = Mock()
		provider.wait_for_server_ready = Mock(side_effect=lambda _server: order("ready"))
		provider.attach_mesh_interface = Mock(side_effect=lambda _server: order("mesh"))

		provider.prepare_server(self.server())

		self.assertEqual([call.args[0] for call in order.call_args_list], ["ready", "mesh"])

	def test_ensure_server_returns_before_it_creates_the_mesh_interface(self) -> None:
		provider = self.provider()
		provider.servers = Mock()
		expected = ProviderServer(
			provider_server_id="i-1",
			status="Installing",
			public_ipv4_address="203.0.113.1",
			provider_metadata={"instance": {"InstanceId": "i-1"}},
		)
		provider.servers.ensure.return_value = expected

		request = ServerCreateRequest(
			name="server-1",
			server_size="c6i.metal",
			server_image="Ubuntu_24.04",
			size_provider_metadata={"BareMetal": True},
			image_provider_metadata={"ImageId": "ami-1"},
		)

		server = provider.ensure_server(request)

		self.assertIs(server, expected)
		provider.servers.ensure_mesh_interface.assert_not_called()

	def test_delete_server_uses_only_the_stored_mesh_interface_id(self) -> None:
		provider = self.provider()

		provider.delete_server("i-1", {"mesh_interface": {"NetworkInterfaceId": "eni-1"}})

		provider.servers.delete.assert_called_once_with("i-1", "eni-1")

	def test_delete_server_uses_no_interface_without_a_stored_id(self) -> None:
		provider = self.provider()

		provider.delete_server("i-1", {"mesh_interface": {"Name": "server-1"}})

		provider.servers.delete.assert_called_once_with("i-1", None)

	def test_attach_mesh_interface_registers_multicast_and_stores_the_address(self) -> None:
		provider = self.provider()
		provider.servers = Mock()
		provider.servers.ensure_mesh_interface.return_value = {"NetworkInterfaceId": "eni-1"}
		provider.servers.fetch_mesh_interface.return_value = {
			"NetworkInterfaceId": "eni-1",
			"PrivateIpAddress": "10.1.0.11",
			"MacAddress": "02:aa:bb:cc:dd:ee",
			"Attachment": {"InstanceId": "i-1", "Status": "attached"},
		}
		server = self.server(provider_server_id="i-1")
		server.provider_metadata = json.dumps({"mesh_interface": {"NetworkInterfaceId": "eni-1"}})

		provider.attach_mesh_interface(server)

		provider.servers.attach_mesh_interface.assert_called_once_with("eni-1", "i-1")
		provider.servers.register_multicast_interface.assert_called_once_with("eni-1")
		self.assertEqual(server.private_ipv4_address, "10.1.0.11")
		self.assertEqual(server.private_network_interface, "atlas-mesh")
		self.assertEqual(provider.mesh_mac_address(server), "02:aa:bb:cc:dd:ee")

	def test_attach_mesh_interface_keeps_the_id_when_attachment_fails(self) -> None:
		provider = self.provider()
		provider.servers = Mock()
		provider.servers.ensure_mesh_interface.return_value = {"NetworkInterfaceId": "eni-1"}
		provider.servers.attach_mesh_interface.side_effect = AwsError("attachment failed")
		server = self.server(provider_server_id="i-1")

		with self.assertRaisesRegex(AwsError, "attachment failed"):
			provider.attach_mesh_interface(server)

		self.assertEqual(
			json.loads(server.provider_metadata)["mesh_interface"],
			{"NetworkInterfaceId": "eni-1"},
		)

	def test_configure_server_network_passes_the_mesh_address_and_mac(self) -> None:
		provider = self.provider()
		server = self.server(provider_server_id="i-1")
		server.private_network_interface = "atlas-mesh"
		server.private_ipv4_address = "10.1.0.11"
		server.provider_metadata = json.dumps({"mesh_interface": {"MacAddress": "02:aa:bb:cc:dd:ee"}})
		provider.uplink_interface = Mock(return_value="ens5")
		provider.run_setup_script = Mock()
		provider.wait_for_private_address = Mock()

		provider.configure_server_network(server)

		environment = provider.run_setup_script.call_args.kwargs["environment"]
		self.assertEqual(environment["DEVICE"], "atlas-mesh")
		self.assertEqual(environment["MAC_ADDRESS"], "02:aa:bb:cc:dd:ee")
		self.assertEqual(environment["ADDRESS"], "10.1.0.11/20")
		self.assertEqual(server.public_network_interface, "ens5")
		provider.wait_for_private_address.assert_called_once_with(server)

	def test_configure_server_network_fails_without_a_mesh_mac_address(self) -> None:
		provider = self.provider()
		server = self.server(provider_server_id="i-1")
		server.private_network_interface = "atlas-mesh"
		server.provider_metadata = "{}"
		provider.uplink_interface = Mock(return_value="ens5")

		with self.assertRaises(AwsError):
			provider.configure_server_network(server)

	def test_metald_binds_the_primary_interface_not_the_mesh_interface(self) -> None:
		provider = self.provider()
		server = self.server()
		server.provider_metadata = json.dumps(
			{
				"instance": {"PrivateIpAddress": "10.1.8.189", "PublicIpAddress": "56.155.92.65"},
				"mesh_interface": {"PrivateIpAddress": "10.1.0.240"},
			}
		)

		self.assertEqual(provider.metald_listen_address(server), "10.1.8.189")

	def test_a_public_address_attaches_to_the_primary_interface(self) -> None:
		provider = self.provider()
		provider.ip_addresses = Mock()
		server = self.server()
		server.provider_metadata = json.dumps(
			{
				"instance": {
					"NetworkInterfaces": [
						{"NetworkInterfaceId": "eni-mesh", "Attachment": {"DeviceIndex": 1}},
						{"NetworkInterfaceId": "eni-primary", "Attachment": {"DeviceIndex": 0}},
					]
				}
			}
		)

		provider.attach_public_ipv4_address("eipalloc-1", "203.0.113.9", server)

		provider.ip_addresses.attach.assert_called_once_with("eipalloc-1", "eni-primary")

	def test_a_public_address_needs_the_primary_interface(self) -> None:
		provider = self.provider()
		server = self.server()
		server.provider_metadata = json.dumps(
			{
				"instance": {
					"NetworkInterfaces": [
						{"NetworkInterfaceId": "eni-mesh", "Attachment": {"DeviceIndex": 1}}
					]
				}
			}
		)

		with self.assertRaisesRegex(AwsError, "primary network interface"):
			provider.attach_public_ipv4_address("eipalloc-1", "203.0.113.9", server)

	def test_metald_needs_the_primary_private_address(self) -> None:
		provider = self.provider()
		server = self.server()
		server.provider_metadata = json.dumps({"instance": {"PublicIpAddress": "56.155.92.65"}})

		with self.assertRaisesRegex(AwsError, "primary private IPv4 address"):
			provider.metald_listen_address(server)

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
		provider = self.provider()
		infrastructure = AwsInfrastructure(provider)

		rules = infrastructure.ingress_rules

		self.assertEqual(
			[(rule["IpProtocol"], rule["IpRanges"][0]["CidrIp"]) for rule in rules],
			[("-1", "0.0.0.0/0")],
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

	def provider(self) -> AwsProvider:
		provider = object.__new__(AwsProvider)
		provider.settings = SimpleNamespace(
			private_network_cidr="10.1.0.0/20",
			private_network_mtu=9001,
			public_ssh_key="ssh-ed25519 key",
			is_server_provider_setup_completed=0,
		)
		provider.configuration = SimpleNamespace()
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

	def test_create_turns_off_network_hotplug(self) -> None:
		servers = self.servers()
		servers.client.paginate.return_value = []
		servers.client.call.return_value = {"Instances": [self.instance()]}

		servers.ensure(self.request())

		user_data = servers.client.call.call_args.kwargs["UserData"]
		self.assertTrue(user_data.startswith("#cloud-config\n"))
		self.assertIn("when: [boot-new-instance]", user_data)

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

	def test_ensure_mesh_interface_uses_the_instance_id_for_idempotency(self) -> None:
		servers = self.servers()
		servers.client.call.return_value = {
			"NetworkInterface": {"NetworkInterfaceId": "eni-1", "PrivateIpAddress": "10.1.0.11"}
		}

		interface = servers.ensure_mesh_interface("i-1", "server-1")

		self.assertEqual(interface["NetworkInterfaceId"], "eni-1")
		servers.client.call.assert_called_once_with(
			"ec2",
			"create_network_interface",
			ClientToken=AwsServers.client_token("mesh-interface", "i-1"),
			SubnetId="subnet-1",
			Groups=["sg-1"],
			Description="Atlas mesh interface for server-1",
			TagSpecifications=[
				{
					"ResourceType": "network-interface",
					"Tags": [{"Key": "atlas-server", "Value": "server-1"}],
				}
			],
		)

	def test_attach_mesh_interface_refuses_an_interface_on_another_instance(self) -> None:
		servers = self.servers()
		servers.client.call.return_value = {
			"NetworkInterfaces": [{"NetworkInterfaceId": "eni-1", "Attachment": {"InstanceId": "i-2"}}]
		}

		with self.assertRaises(AwsError):
			servers.attach_mesh_interface("eni-1", "i-1")

	def test_mesh_interface_is_deleted_with_the_instance(self) -> None:
		servers = self.servers()
		interface = {
			"NetworkInterfaceId": "eni-1",
			"Attachment": {"AttachmentId": "eni-attach-1"},
		}

		servers.configure_mesh_interface(interface)

		attachment = servers.client.call.call_args_list[0].kwargs["Attachment"]
		self.assertEqual(attachment, {"AttachmentId": "eni-attach-1", "DeleteOnTermination": True})

	def test_the_mesh_interface_is_registered_as_a_member_and_a_source(self) -> None:
		servers = self.servers()

		servers.register_multicast_interface("eni-1")

		operations = [call.args[1] for call in servers.client.call.call_args_list]
		self.assertEqual(
			operations,
			[
				"register_transit_gateway_multicast_group_members",
				"register_transit_gateway_multicast_group_sources",
			],
		)
		self.assertEqual(servers.client.call.call_args.kwargs["GroupIpAddress"], MESH_MULTICAST_GROUP)

	def test_registration_accepts_an_interface_it_already_registered(self) -> None:
		servers = self.servers()

		servers.register_multicast_interface("eni-1")

		for call in servers.client.call.call_args_list:
			self.assertTrue(call.kwargs["allow_existing"])

	def test_registration_fails_without_a_multicast_domain(self) -> None:
		servers = self.servers(multicast_domain_id=None)

		with self.assertRaises(AwsError):
			servers.register_multicast_interface("eni-1")

	def test_the_power_action_uses_the_explicit_operation(self) -> None:
		servers = self.servers()

		servers.set_power_state("i-1", ServerPowerAction.STOP)

		self.assertEqual(servers.client.call.call_args.args[1], "stop_instances")
		self.assertEqual(servers.client.call.call_args.kwargs["InstanceIds"], ["i-1"])

	def test_delete_releases_multicast_and_deletes_attached_interface_with_instance(self) -> None:
		servers = self.servers()
		servers.client.call.return_value = {
			"NetworkInterfaces": [
				{
					"NetworkInterfaceId": "eni-1",
					"Attachment": {
						"AttachmentId": "eni-attach-1",
						"InstanceId": "i-1",
					},
				}
			]
		}

		servers.delete("i-1", "eni-1")

		operations = [call.args[1] for call in servers.client.call.call_args_list]
		self.assertIn("deregister_transit_gateway_multicast_group_members", operations)
		self.assertIn("deregister_transit_gateway_multicast_group_sources", operations)
		self.assertIn("modify_network_interface_attribute", operations)
		modify_call = servers.client.call.call_args_list[-2]
		self.assertEqual(
			modify_call.kwargs["Attachment"],
			{"AttachmentId": "eni-attach-1", "DeleteOnTermination": True},
		)
		self.assertEqual(operations[-1], "terminate_instances")
		self.assertTrue(servers.client.call.call_args.kwargs["allow_missing"])

	def test_delete_removes_an_unattached_mesh_interface(self) -> None:
		servers = self.servers()
		servers.client.call.return_value = {"NetworkInterfaces": [{"NetworkInterfaceId": "eni-1"}]}

		servers.delete("i-1", "eni-1")

		operations = [call.args[1] for call in servers.client.call.call_args_list]
		self.assertEqual(
			operations,
			[
				"describe_network_interfaces",
				"deregister_transit_gateway_multicast_group_members",
				"deregister_transit_gateway_multicast_group_sources",
				"delete_network_interface",
				"terminate_instances",
			],
		)
		delete_call = servers.client.call.call_args_list[-2]
		self.assertEqual(delete_call.kwargs["NetworkInterfaceId"], "eni-1")
		self.assertTrue(delete_call.kwargs["allow_missing"])

	def test_delete_uses_no_interface_when_atlas_has_no_interface_id(self) -> None:
		servers = self.servers()

		servers.delete("i-1", None)

		servers.client.call.assert_called_once_with(
			"ec2", "terminate_instances", InstanceIds=["i-1"], allow_missing=True
		)

	def test_delete_refuses_a_mesh_interface_on_another_instance(self) -> None:
		servers = self.servers()
		servers.client.call.return_value = {
			"NetworkInterfaces": [
				{
					"NetworkInterfaceId": "eni-1",
					"Attachment": {"AttachmentId": "eni-attach-1", "InstanceId": "i-2"},
				}
			]
		}

		with self.assertRaisesRegex(AwsError, "belongs to instance i-2"):
			servers.delete("i-1", "eni-1")

		operations = [call.args[1] for call in servers.client.call.call_args_list]
		self.assertEqual(operations, ["describe_network_interfaces"])

	def test_delete_refuses_a_different_interface_than_the_stored_id(self) -> None:
		servers = self.servers()
		servers.client.call.return_value = {"NetworkInterfaces": [{"NetworkInterfaceId": "eni-2"}]}

		with self.assertRaisesRegex(AwsError, "for requested ID eni-1"):
			servers.delete("i-1", "eni-1")

		operations = [call.args[1] for call in servers.client.call.call_args_list]
		self.assertEqual(operations, ["describe_network_interfaces"])

	@staticmethod
	def servers(*, multicast_domain_id: str | None = "tgw-mcast-1") -> AwsServers:
		return AwsServers(
			client=Mock(),
			configuration=SimpleNamespace(
				subnet_id="subnet-1",
				security_group_id="sg-1",
				key_pair_name="atlas-eu-ssh-key",
				multicast_domain_id=multicast_domain_id,
				resource_name_prefix="atlas-eu-",
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

	def test_delete_is_idempotent(self) -> None:
		addresses = self.addresses()

		addresses.delete("eipalloc-1")

		self.assertTrue(addresses.client.call.call_args.kwargs["allow_missing"])

	@staticmethod
	def addresses() -> AwsIPAddresses:
		return AwsIPAddresses(client=Mock(), configuration=SimpleNamespace(resource_name_prefix="atlas-eu-"))
