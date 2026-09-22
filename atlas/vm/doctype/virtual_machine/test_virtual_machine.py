from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.vm.core import vm_service as virtual_machine_service_module
from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.metal_models import MetalVirtualMachine
from atlas.vm.core.models import FirewallConfiguration, FirewallRule, VirtualMachineCreateRequest
from atlas.vm.core.vm_service import VirtualMachineService
from atlas.vm.doctype.virtual_machine import virtual_machine as virtual_machine_module
from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine

METAL_VIRTUAL_MACHINE_RESPONSE = {
	"id": "VM-00001",
	"desired": {
		"generation": 2,
		"restart_generation": 1,
		"state": "running",
		"compute": {
			"cpu_millicores": 2000,
			"memory_mib": 2048,
			"sleep_after_idle_seconds": 1800,
		},
		"disk": {"size_mib": 2048, "throughput_mibps": 50, "iops": 2000},
		"image": {
			"ref": "ubuntu",
			"architecture": "amd64",
			"rootfs": {"sha256": "a" * 64},
			"kernel": {"sha256": "b" * 64},
			"cache_image": False,
			"memory_snapshot": False,
			"memory_snapshot_configuration": None,
		},
		"network": {
			"egress": "uplink",
			"public_ipv4": "203.0.113.10",
			"wireguard_mesh_ipv6": "fdaa:1::1",
			"private_network_throughput_mibps": 100,
			"public_network_throughput_mibps": 50,
			"firewall": {"enabled": False, "inbound": [], "outbound": []},
		},
		"guest": {
			"hostname": "worker-1",
			"ssh_keys": ["ssh-ed25519 AAAA"],
			"metadata": {"env": "prod"},
		},
	},
	"observed": {
		"generation": 1,
		"restart_generation": 1,
		"state": "running",
		"phase": "network",
		"operation_id": "operation-1",
		"operation_started_at": "2026-09-05T10:00:00Z",
		"updated_at": "2026-09-05T10:00:02Z",
		"disk": {"used_mib": 1024},
		"network": {"mac": "06:00:00:00:00:01"},
		"error": None,
	},
}


class TestVirtualMachineRequest(UnitTestCase):
	def test_request_parses_ssh_keys_and_defaults(self) -> None:
		request = VirtualMachineCreateRequest.from_value(
			{
				"virtual_machine_image": "Ubuntu 24.04",
				"cpu_millicores": 1500,
				"memory_mib": 2048,
				"disk_mib": 10240,
				"tenant_id": 7,
				"ssh_keys": "key-one\nkey-two",
			}
		)

		self.assertEqual(request.ssh_keys, ("key-one", "key-two"))
		self.assertEqual(request.cpu_millicores, 1500)
		self.assertEqual(request.egress, "uplink")
		self.assertEqual(request.firewall, FirewallConfiguration())

	def test_request_parses_a_firewall(self) -> None:
		request = VirtualMachineCreateRequest.from_value(
			{
				"virtual_machine_image": "Ubuntu 24.04",
				"cpu_millicores": 1500,
				"memory_mib": 2048,
				"disk_mib": 10240,
				"tenant_id": 7,
				"firewall": {
					"enabled": True,
					"inbound": [{"protocol": "tcp", "ports": "22", "cidrs": ["203.0.113.0/24"]}],
				},
			}
		)

		self.assertTrue(request.firewall.enabled)
		self.assertEqual(
			request.firewall.inbound,
			(FirewallRule(protocol="tcp", ports="22", cidrs=("203.0.113.0/24",)),),
		)

	def test_request_rejects_a_noncanonical_firewall_cidr(self) -> None:
		with self.assertRaisesRegex(ValueError, "canonical"):
			VirtualMachineCreateRequest.from_value(
				{
					"virtual_machine_image": "Ubuntu 24.04",
					"cpu_millicores": 1500,
					"memory_mib": 2048,
					"disk_mib": 10240,
					"tenant_id": 7,
					"firewall": {"inbound": [{"protocol": "any", "cidrs": ["203.0.113.7/24"]}]},
				}
			)

	def test_request_limits_firewall_prefix_entries(self) -> None:
		with self.assertRaisesRegex(ValueError, "50 prefix entries"):
			VirtualMachineCreateRequest.from_value(
				{
					"virtual_machine_image": "Ubuntu 24.04",
					"cpu_millicores": 1500,
					"memory_mib": 2048,
					"disk_mib": 10240,
					"tenant_id": 7,
					"firewall": {
						"inbound": [
							{
								"protocol": "any",
								"cidrs": [f"10.0.0.{index}/32" for index in range(51)],
							}
						]
					},
				}
			)

	def test_request_accepts_the_infrastructure_tenant(self) -> None:
		request = VirtualMachineCreateRequest.from_value(
			{
				"virtual_machine_image": "Ubuntu 24.04",
				"cpu_millicores": 1000,
				"memory_mib": 2048,
				"disk_mib": 10240,
				"tenant_id": 0,
			}
		)

		self.assertEqual(request.tenant_id, 0)

	def test_request_rejects_cpu_above_the_firecracker_limit(self) -> None:
		with self.assertRaisesRegex(ValueError, "must not exceed 32000"):
			VirtualMachineCreateRequest.from_value(
				{
					"virtual_machine_image": "Ubuntu 24.04",
					"cpu_millicores": 32001,
					"memory_mib": 2048,
					"disk_mib": 10240,
					"tenant_id": 7,
				}
			)

	def test_request_rejects_cpu_below_the_minimum(self) -> None:
		with self.assertRaisesRegex(ValueError, "must be at least 100"):
			VirtualMachineCreateRequest.from_value(
				{
					"virtual_machine_image": "Ubuntu 24.04",
					"cpu_millicores": 99,
					"memory_mib": 2048,
					"disk_mib": 10240,
					"tenant_id": 7,
				}
			)

	def test_request_parses_metadata(self) -> None:
		request = VirtualMachineCreateRequest.from_value(
			{
				"virtual_machine_image": "Ubuntu 24.04",
				"cpu_millicores": 2000,
				"memory_mib": 2048,
				"disk_mib": 10240,
				"tenant_id": 7,
				"metadata": {" env ": "prod", "team": "platform"},
			}
		)

		self.assertEqual(request.metadata, {"env": "prod", "team": "platform"})

	def test_request_rejects_empty_metadata_key(self) -> None:
		with self.assertRaises(ValueError):
			VirtualMachineCreateRequest.from_value(
				{
					"virtual_machine_image": "Ubuntu 24.04",
					"cpu_millicores": 2000,
					"memory_mib": 2048,
					"disk_mib": 10240,
					"tenant_id": 7,
					"metadata": {"": "value"},
				}
			)

	def test_request_parses_disk_limits(self) -> None:
		request = VirtualMachineCreateRequest.from_value(
			{
				"virtual_machine_image": "Ubuntu 24.04",
				"cpu_millicores": 2000,
				"memory_mib": 2048,
				"disk_mib": 10240,
				"tenant_id": 7,
				"disk_throughput_mibps": 50,
				"disk_iops": 2000,
			}
		)

		self.assertEqual(request.disk_throughput_mibps, 50)
		self.assertEqual(request.disk_iops, 2000)

	def test_request_parses_throughput_limits(self) -> None:
		request = VirtualMachineCreateRequest.from_value(
			{
				"virtual_machine_image": "Ubuntu 24.04",
				"cpu_millicores": 2000,
				"memory_mib": 2048,
				"disk_mib": 10240,
				"tenant_id": 7,
				"private_network_throughput_mibps": 100,
			}
		)

		self.assertEqual(request.private_network_throughput_mibps, 100)
		self.assertEqual(request.public_network_throughput_mibps, 0)

	def test_request_accepts_mesh_egress(self) -> None:
		request = VirtualMachineCreateRequest.from_value(
			{
				"virtual_machine_image": "Ubuntu 24.04",
				"cpu_millicores": 2000,
				"memory_mib": 2048,
				"disk_mib": 10240,
				"tenant_id": 7,
				"egress": "mesh",
				"private_network_throughput_mibps": 100,
			}
		)

		self.assertEqual(request.egress, "mesh")
		self.assertEqual(request.private_network_throughput_mibps, 100)

	def test_request_rejects_a_public_address_without_uplink(self) -> None:
		base = {
			"virtual_machine_image": "Ubuntu 24.04",
			"cpu_millicores": 2000,
			"memory_mib": 2048,
			"disk_mib": 10240,
			"tenant_id": 7,
			"egress": "mesh",
		}
		with self.assertRaises(ValueError):
			VirtualMachineCreateRequest.from_value({**base, "server_ip_address": "203.0.113.10"})

		# Store the public limit; a mode change needs no cleanup.
		request = VirtualMachineCreateRequest.from_value({**base, "public_network_throughput_mibps": 50})
		self.assertEqual(request.public_network_throughput_mibps, 50)

	def test_request_rejects_negative_throughput(self) -> None:
		with self.assertRaises(ValueError):
			VirtualMachineCreateRequest.from_value(
				{
					"virtual_machine_image": "Ubuntu 24.04",
					"cpu_millicores": 2000,
					"memory_mib": 2048,
					"disk_mib": 10240,
					"tenant_id": 7,
					"public_network_throughput_mibps": -1,
				}
			)

	def test_request_rejects_boolean_capacity(self) -> None:
		with self.assertRaises(ValueError):
			VirtualMachineCreateRequest.from_value(
				{
					"virtual_machine_image": "Ubuntu 24.04",
					"cpu_millicores": True,
					"memory_mib": 2048,
					"disk_mib": 10240,
					"tenant_id": 7,
				}
			)

	def test_request_rejects_boolean_tenant_id(self) -> None:
		with self.assertRaises(ValueError):
			VirtualMachineCreateRequest.from_value(
				{
					"virtual_machine_image": "Ubuntu 24.04",
					"cpu_millicores": 2000,
					"memory_mib": 2048,
					"disk_mib": 10240,
					"tenant_id": True,
				}
			)


class TestVirtualMachineDocument(UnitTestCase):
	def test_autoname_assigns_permanent_virtual_machine_id(self) -> None:
		virtual_machine = frappe.new_doc("Virtual Machine")

		with patch.object(
			virtual_machine_module, "make_autoname", return_value="vm-0000042"
		) as make_autoname:
			virtual_machine.autoname()

		self.assertEqual(virtual_machine.name, "vm-0000042")
		make_autoname.assert_called_once_with("vm-.#######", doc=virtual_machine)

	# New records have no Server, so virtual-field reads must skip Metal lookup.
	def test_new_document_reads_virtual_fields_without_a_server(self) -> None:
		virtual_machine = frappe.new_doc("Virtual Machine")

		self.assertIsNone(virtual_machine.get_metal_vm_info())
		self.assertEqual(virtual_machine.current_state, "unknown")
		self.assertIsNone(virtual_machine.desired_state)


class TestVirtualMachineResize(UnitTestCase):
	def build_virtual_machine(self, *, is_terminating: int = 0) -> VirtualMachine:
		virtual_machine = VirtualMachine.__new__(VirtualMachine)
		virtual_machine.name = "VM-00001"
		virtual_machine.is_draft = 0
		virtual_machine.is_terminating = is_terminating
		virtual_machine.check_permission = Mock()
		virtual_machine.ensure_not_migrating = Mock()
		return virtual_machine

	def test_rejects_a_terminating_virtual_machine(self) -> None:
		virtual_machine = self.build_virtual_machine(is_terminating=1)

		with self.assertRaisesRegex(AtlasUserError, "terminating"):
			virtual_machine.resize(memory_mib=4096)

	def test_rejects_a_non_integer_value(self) -> None:
		virtual_machine = self.build_virtual_machine()

		for value in (True, 1.5, "1.5"):
			with self.assertRaisesRegex(AtlasUserError, "must be integers"):
				virtual_machine.resize(sleep_after_idle_seconds=value)

	def test_resize_disk_rejects_a_non_integer_value(self) -> None:
		virtual_machine = self.build_virtual_machine()

		for value in (True, 1.5, "1.5"):
			with self.assertRaisesRegex(AtlasUserError, "whole number"):
				virtual_machine.resize_disk(value)


class TestVirtualMachineService(UnitTestCase):
	def test_create_request_waits_for_the_provider_host_address(self) -> None:
		request = VirtualMachineCreateRequest("machine-image", 2000, 2048, 10240, 7)
		image = SimpleNamespace(get_metal_image_request=Mock(return_value={}))
		virtual_machine = SimpleNamespace(tenant_id=7, name="VM-00001")
		server_ip_address = SimpleNamespace(host_address=None)

		with patch.object(
			virtual_machine_service_module, "get_virtual_machine_mesh_address", return_value="fdaa::1"
		):
			metal_request = VirtualMachineService(virtual_machine).get_metal_request(
				request, image, server_ip_address
			)

		self.assertEqual(metal_request["network"]["public_ipv4"], "")

	def test_machine_image_uses_its_own_artifacts(self) -> None:
		request = VirtualMachineCreateRequest("machine-image", 2000, 2048, 10240, 7)
		image_request = {
			"ref": "sha256:machine",
			"architecture": "amd64",
			"rootfs": {"url": "machine-rootfs", "sha256": "a" * 64},
			"kernel": {"url": "machine-kernel", "sha256": "b" * 64},
		}
		image = SimpleNamespace(get_metal_image_request=Mock(return_value=image_request))
		virtual_machine = SimpleNamespace(tenant_id=7, name="VM-00001")

		with patch.object(
			virtual_machine_service_module, "get_virtual_machine_mesh_address", return_value="fdaa::1"
		):
			metal_request = VirtualMachineService(virtual_machine).get_metal_request(request, image, None)

		self.assertEqual(metal_request["image"], image_request)
		image.get_metal_image_request.assert_called_once_with()

	def test_metal_request_carries_throughput_limits(self) -> None:
		request = VirtualMachineCreateRequest(
			"machine-image",
			2000,
			2048,
			10240,
			7,
			private_network_throughput_mibps=100,
			public_network_throughput_mibps=50,
		)
		image = SimpleNamespace(get_metal_image_request=Mock(return_value={}))
		virtual_machine = SimpleNamespace(tenant_id=7, name="VM-00001")

		with patch.object(
			virtual_machine_service_module, "get_virtual_machine_mesh_address", return_value="fdaa::1"
		):
			metal_request = VirtualMachineService(virtual_machine).get_metal_request(request, image, None)

		self.assertEqual(
			metal_request["compute"],
			{
				"cpu_millicores": 2000,
				"memory_mib": 2048,
				"sleep_after_idle_seconds": 0,
			},
		)
		self.assertEqual(metal_request["disk"]["size_mib"], 10240)
		self.assertEqual(metal_request["guest"]["ssh_keys"], [])
		self.assertEqual(metal_request["network"]["private_network_throughput_mibps"], 100)
		self.assertEqual(metal_request["network"]["public_network_throughput_mibps"], 50)
		self.assertEqual(
			metal_request["network"]["firewall"], {"enabled": False, "inbound": [], "outbound": []}
		)


class TestVirtualMachineVirtualFields(UnitTestCase):
	def test_the_public_address_comes_from_the_atlas_record(self) -> None:
		virtual_machine = VirtualMachine.__new__(VirtualMachine)
		virtual_machine.name = "vm-0000001"

		with patch(
			"atlas.vm.doctype.virtual_machine.virtual_machine.frappe.db.get_value",
			return_value="203.0.113.10",
		) as get_value:
			self.assertEqual(virtual_machine.public_ipv4, "203.0.113.10")

		self.assertEqual(get_value.call_args.args[0], "Metal Server IP Address")
		self.assertEqual(get_value.call_args.args[1], {"virtual_machine": "vm-0000001"})

	def test_virtual_fields_read_the_nested_model(self) -> None:
		virtual_machine = VirtualMachine.__new__(VirtualMachine)
		virtual_machine.is_draft = 0
		virtual_machine.is_terminating = 0
		information = MetalVirtualMachine.from_dict(METAL_VIRTUAL_MACHINE_RESPONSE)
		virtual_machine.get_metal_vm_info = Mock(return_value=information)

		self.assertEqual(virtual_machine.current_state, "running")
		self.assertEqual(virtual_machine.desired_state, "running")
		self.assertEqual(virtual_machine.hostname, "worker-1")
		self.assertEqual(virtual_machine.mac, "06:00:00:00:00:01")
		self.assertEqual(virtual_machine.egress, "uplink")
		self.assertEqual(virtual_machine.wireguard_mesh_ipv6, "fdaa:1::1")
		self.assertEqual(virtual_machine.disk_throughput_mibps, 50)
		self.assertEqual(virtual_machine.disk_iops, 2000)
		self.assertEqual(virtual_machine.private_network_throughput_mibps, 100)
		self.assertEqual(virtual_machine.public_network_throughput_mibps, 50)
		self.assertEqual(virtual_machine.firewall_summary, "")
		self.assertEqual(virtual_machine.ssh_keys, "ssh-ed25519 AAAA")
		self.assertEqual(virtual_machine.metadata, '{\n  "env": "prod"\n}')

	def test_read_firewall_returns_the_nested_model(self) -> None:
		virtual_machine = VirtualMachine.__new__(VirtualMachine)
		information = MetalVirtualMachine.from_dict(METAL_VIRTUAL_MACHINE_RESPONSE)
		virtual_machine.check_permission = Mock()
		virtual_machine.get_metal_vm_info = Mock(return_value=information)

		firewall = virtual_machine.read_firewall()

		virtual_machine.check_permission.assert_called_once_with("read")
		self.assertEqual(firewall, {"enabled": False, "inbound": [], "outbound": []})


class TestVirtualMachineNetwork(UnitTestCase):
	"""Cover the live network updates that do not restart the VM."""

	def build_virtual_machine(self, network: dict) -> tuple[VirtualMachine, Mock]:
		virtual_machine = VirtualMachine.__new__(VirtualMachine)
		virtual_machine.name = "VM-00001"
		virtual_machine.server = "node-1"
		virtual_machine.is_draft = 0
		virtual_machine.is_terminating = 0
		virtual_machine.active_migration = None

		response_value = {
			**METAL_VIRTUAL_MACHINE_RESPONSE,
			"desired": {
				**METAL_VIRTUAL_MACHINE_RESPONSE["desired"],
				"network": {
					**METAL_VIRTUAL_MACHINE_RESPONSE["desired"]["network"],
					**network,
				},
			},
		}
		information = MetalVirtualMachine.from_dict(response_value)
		client = Mock()
		client.get_virtual_machine.return_value = information
		client.set_virtual_machine_network.return_value = information
		return virtual_machine, client

	def patches(self, client: Mock, address_name: str | None) -> tuple[Any, ...]:
		return (
			patch.object(virtual_machine_service_module, "MetalClient", return_value=client),
			patch.object(virtual_machine_module.frappe, "get_doc", return_value=Mock()),
			patch.object(VirtualMachine, "check_permission"),
			patch.object(
				virtual_machine_module.frappe,
				"db",
				Mock(
					exists=Mock(return_value=address_name),
					get_value=Mock(return_value=address_name),
				),
			),
		)

	def test_update_firewall_passes_one_dictionary_shape(self) -> None:
		virtual_machine = VirtualMachine.__new__(VirtualMachine)
		virtual_machine.update_network = Mock(return_value={})
		firewall = {"enabled": True, "inbound": [], "outbound": []}

		virtual_machine.update_firewall(firewall)

		virtual_machine.update_network.assert_called_once_with({"firewall": firewall})

	def test_update_network_keeps_the_unchanged_metal_values(self) -> None:
		virtual_machine, client = self.build_virtual_machine(
			{
				"egress": "uplink",
				"public_ipv4": "203.0.113.10",
				"wireguard_mesh_ipv6": "fdaa:1::1",
				"private_network_throughput_mibps": 100,
				"public_network_throughput_mibps": 50,
			}
		)

		with (
			patch.object(virtual_machine_service_module, "MetalClient", return_value=client),
			patch.object(virtual_machine_module.frappe, "get_doc", return_value=Mock()),
		):
			VirtualMachineService(virtual_machine).apply_network_changes(
				{"public_network_throughput_mibps": 25}
			)

		client.set_virtual_machine_network.assert_called_once_with(
			"VM-00001",
			{
				"egress": "uplink",
				"public_ipv4": "203.0.113.10",
				"wireguard_mesh_ipv6": "fdaa:1::1",
				"private_network_throughput_mibps": 100,
				"public_network_throughput_mibps": 25,
				"firewall": {"enabled": False, "inbound": [], "outbound": []},
			},
		)

	def test_update_network_merges_partial_firewall_fields(self) -> None:
		virtual_machine, client = self.build_virtual_machine(
			{
				"firewall": {
					"enabled": False,
					"inbound": [{"protocol": "tcp", "ports": "22", "cidrs": ["203.0.113.0/24"]}],
					"outbound": [{"protocol": "any", "cidrs": ["0.0.0.0/0", "::/0"]}],
				}
			}
		)

		with (
			patch.object(virtual_machine_service_module, "MetalClient", return_value=client),
			patch.object(virtual_machine_module.frappe, "get_doc", return_value=Mock()),
		):
			VirtualMachineService(virtual_machine).apply_network_changes({"firewall": {"enabled": True}})

		firewall = client.set_virtual_machine_network.call_args.args[1]["firewall"]
		self.assertTrue(firewall["enabled"])
		self.assertEqual(firewall["inbound"][0]["ports"], "22")
		self.assertEqual(firewall["outbound"][0]["cidrs"], ["0.0.0.0/0", "::/0"])

	def test_update_network_rejects_an_invalid_partial_firewall(self) -> None:
		virtual_machine, client = self.build_virtual_machine({})

		with (
			patch.object(virtual_machine_service_module, "MetalClient", return_value=client),
			patch.object(virtual_machine_module.frappe, "get_doc", return_value=Mock()),
			self.assertRaisesRegex(frappe.ValidationError, "between 1 and 65535"),
		):
			VirtualMachineService(virtual_machine).apply_network_changes(
				{"firewall": {"inbound": [{"protocol": "tcp", "ports": "0", "cidrs": ["0.0.0.0/0"]}]}}
			)

		client.set_virtual_machine_network.assert_not_called()

	def test_update_network_stops_when_metal_request_fails(self) -> None:
		virtual_machine, client = self.build_virtual_machine({})
		client.get_virtual_machine.side_effect = MetalClientError("invalid response")

		with (
			patch.object(virtual_machine_service_module, "MetalClient", return_value=client),
			patch.object(virtual_machine_module.frappe, "get_doc", return_value=Mock()),
			self.assertRaises(frappe.ValidationError),
		):
			VirtualMachineService(virtual_machine).apply_network_changes(
				{"public_network_throughput_mibps": 25}
			)

		client.set_virtual_machine_network.assert_not_called()

	def test_update_network_rejects_a_draft(self) -> None:
		virtual_machine, client = self.build_virtual_machine({"egress": "uplink"})
		virtual_machine.is_draft = 1

		with (
			patch.object(virtual_machine_service_module, "MetalClient", return_value=client),
			self.assertRaises(frappe.ValidationError),
		):
			VirtualMachineService(virtual_machine).apply_network_changes(
				{"public_network_throughput_mibps": 25}
			)

	def test_attach_ip_address_waits_for_the_host_address(self) -> None:
		virtual_machine, client = self.build_virtual_machine({"egress": "none", "public_ipv4": ""})
		address = SimpleNamespace(address="203.0.113.10", host_address=None)
		metal_client, get_doc, check_permission, database = self.patches(client, None)

		with (
			metal_client,
			get_doc,
			check_permission,
			database,
			patch.object(VirtualMachineService, "assign_ip_address", return_value=address) as assign,
		):
			virtual_machine.attach_ip_address("203.0.113.10")

		assign.assert_called_once_with("203.0.113.10")
		request = client.set_virtual_machine_network.call_args.args[1]
		self.assertEqual(request["egress"], "uplink")
		self.assertEqual(request["public_ipv4"], "")

	def test_attach_ip_address_rejects_a_second_address(self) -> None:
		virtual_machine, client = self.build_virtual_machine({"egress": "uplink"})
		metal_client, get_doc, check_permission, database = self.patches(client, "203.0.113.10")

		with metal_client, get_doc, check_permission, database, self.assertRaises(frappe.ValidationError):
			virtual_machine.attach_ip_address("203.0.113.11")

		client.set_virtual_machine_network.assert_not_called()

	def test_detach_ip_address_updates_metal_before_the_release(self) -> None:
		virtual_machine, client = self.build_virtual_machine(
			{"egress": "uplink", "public_ipv4": "203.0.113.10"}
		)
		calls: list[str] = []
		information = client.set_virtual_machine_network.return_value
		client.set_virtual_machine_network.side_effect = lambda *arguments: (
			calls.append("metal") or information
		)
		metal_client, get_doc, check_permission, database = self.patches(client, "203.0.113.10")

		with (
			metal_client,
			get_doc,
			check_permission,
			database,
			patch.object(
				VirtualMachineService,
				"release_ip_address",
				side_effect=lambda self=None: calls.append("release"),
			),
		):
			virtual_machine.detach_ip_address()

		self.assertEqual(calls, ["metal", "release"])
		request = client.set_virtual_machine_network.call_args.args[1]
		self.assertEqual(request["public_ipv4"], "")
		self.assertEqual(request["egress"], "uplink")

	def test_update_egress_sends_the_new_mode(self) -> None:
		virtual_machine, client = self.build_virtual_machine({"egress": "uplink"})
		metal_client, get_doc, check_permission, database = self.patches(client, None)

		with metal_client, get_doc, check_permission, database:
			virtual_machine.update_egress("mesh")

		self.assertEqual(client.set_virtual_machine_network.call_args.args[1]["egress"], "mesh")

	def test_update_egress_rejects_an_unknown_mode(self) -> None:
		virtual_machine, client = self.build_virtual_machine({"egress": "uplink"})
		metal_client, get_doc, check_permission, database = self.patches(client, None)

		with metal_client, get_doc, check_permission, database, self.assertRaises(frappe.ValidationError):
			virtual_machine.update_egress("server")

		client.set_virtual_machine_network.assert_not_called()

	def test_update_egress_keeps_the_internet_path_for_an_attached_address(self) -> None:
		"""A public IPv4 address needs uplink, so Atlas refuses mesh and none."""
		virtual_machine, client = self.build_virtual_machine(
			{"egress": "uplink", "public_ipv4": "203.0.113.10"}
		)

		for egress in ("mesh", "none"):
			metal_client, get_doc, check_permission, database = self.patches(client, "203.0.113.10")
			with metal_client, get_doc, check_permission, database, self.assertRaises(frappe.ValidationError):
				virtual_machine.update_egress(egress)

		client.set_virtual_machine_network.assert_not_called()

	def test_update_network_throughput_rejects_bad_values(self) -> None:
		"""A malformed value must fail, not silently become 0 and remove the limit."""
		virtual_machine, client = self.build_virtual_machine({"egress": "uplink"})

		for private, public in ((-1, 0), ("abc", 0), (0, "")):
			metal_client, get_doc, check_permission, database = self.patches(client, None)
			with metal_client, get_doc, check_permission, database, self.assertRaises(frappe.ValidationError):
				virtual_machine.update_network_throughput(private, public)

		client.set_virtual_machine_network.assert_not_called()

	def test_update_disk_limits_names_the_failing_limit(self) -> None:
		"""The IOPS limit must not report a throughput unit."""
		virtual_machine, client = self.build_virtual_machine({"egress": "uplink"})
		metal_client, get_doc, check_permission, database = self.patches(client, None)

		with (
			metal_client,
			get_doc,
			check_permission,
			database,
			self.assertRaisesRegex(frappe.ValidationError, "Disk IOPS"),
		):
			virtual_machine.update_disk_limits(0, "abc")

	def test_update_disk_rejects_a_draft(self) -> None:
		virtual_machine, client = self.build_virtual_machine({"egress": "uplink"})
		virtual_machine.is_draft = 1
		metal_client, get_doc, check_permission, database = self.patches(client, None)

		with metal_client, get_doc, check_permission, database, self.assertRaises(frappe.ValidationError):
			virtual_machine.update_disk({"size_mib": 40960})

		client.set_virtual_machine_disk.assert_not_called()

	def test_update_egress_keeps_a_stored_public_limit(self) -> None:
		"""Atlas resends every setting, so a stored public limit must not block a mode change."""
		virtual_machine, client = self.build_virtual_machine(
			{"egress": "uplink", "public_network_throughput_mibps": 31}
		)
		metal_client, get_doc, check_permission, database = self.patches(client, None)

		with metal_client, get_doc, check_permission, database:
			virtual_machine.update_egress("mesh")

		request = client.set_virtual_machine_network.call_args.args[1]
		self.assertEqual(request["egress"], "mesh")
		self.assertEqual(request["public_network_throughput_mibps"], 31)


class TestVirtualMachineTrash(UnitTestCase):
	"""Cover the cleanup that lets a terminated VM record be deleted."""

	def _trash(self, state_exists: bool, migrations: list[str] | None = None) -> Mock:
		virtual_machine = Mock(doctype="Virtual Machine")
		virtual_machine.name = "vm-00003"

		with (
			patch.object(virtual_machine_module, "VirtualMachineService") as service,
			patch.object(virtual_machine_module, "delete_tasks_for_target") as delete_tasks,
			patch.object(virtual_machine_module.frappe.db, "exists", return_value=state_exists),
			patch.object(virtual_machine_module.frappe, "get_all", return_value=migrations or []),
			patch.object(virtual_machine_module.frappe, "delete_doc") as delete_doc,
		):
			virtual_machine_module.VirtualMachine.on_trash(virtual_machine)

		service.return_value.validate_deletion.assert_called_once()
		delete_tasks.assert_called_once_with("Virtual Machine", "vm-00003")
		return delete_doc

	def test_trash_removes_dependent_records(self) -> None:
		"""Dependent records must not prevent the virtual machine deletion."""
		delete_doc = self._trash(state_exists=True)

		delete_doc.assert_called_once_with(
			"Virtual Machine State", "vm-00003", ignore_permissions=True, delete_permanently=True
		)

	def test_trash_skips_a_missing_state(self) -> None:
		"""A virtual machine that never reported a state still deletes."""
		self._trash(state_exists=False).assert_not_called()

	def test_trash_removes_the_migration_history(self) -> None:
		"""A migration links to its VM, so the history must go with the VM."""
		delete_doc = self._trash(state_exists=False, migrations=["mig-00001", "mig-00002"])

		self.assertEqual(
			delete_doc.call_args_list,
			[
				call(
					"Virtual Machine Migration",
					"mig-00001",
					ignore_permissions=True,
					delete_permanently=True,
				),
				call(
					"Virtual Machine Migration",
					"mig-00002",
					ignore_permissions=True,
					delete_permanently=True,
				),
			],
		)


class TestVirtualMachineTerminationProtection(UnitTestCase):
	"""Cover the flag that refuses removal of a virtual machine."""

	def build_virtual_machine(self, *, is_termination_protected: int = 0) -> VirtualMachine:
		virtual_machine = VirtualMachine.__new__(VirtualMachine)
		virtual_machine.name = "VM-00001"
		virtual_machine.is_terminating = 0
		virtual_machine.is_termination_protected = is_termination_protected
		virtual_machine.active_migration = None
		virtual_machine.check_permission = Mock()
		virtual_machine.save = Mock()
		return virtual_machine

	def test_a_protected_virtual_machine_is_not_terminated(self) -> None:
		virtual_machine = self.build_virtual_machine(is_termination_protected=1)

		with (
			patch.object(virtual_machine_module, "VirtualMachineService") as service,
			self.assertRaises(AtlasUserError),
		):
			virtual_machine.terminate()

		service.return_value.terminate.assert_not_called()

	def test_a_protected_virtual_machine_is_not_deleted(self) -> None:
		"""The guard must also stop a direct delete, not only the terminate action."""
		virtual_machine = self.build_virtual_machine(is_termination_protected=1)
		virtual_machine.doctype = "Virtual Machine"

		with (
			patch.object(virtual_machine_module, "VirtualMachineService") as service,
			self.assertRaises(AtlasUserError),
		):
			virtual_machine.on_trash()

		service.return_value.validate_deletion.assert_not_called()

	def test_an_unprotected_virtual_machine_is_terminated(self) -> None:
		virtual_machine = self.build_virtual_machine()

		with patch.object(virtual_machine_module, "VirtualMachineService") as service:
			virtual_machine.terminate()

		service.return_value.terminate.assert_called_once()

	def test_protection_is_set_and_cleared(self) -> None:
		virtual_machine = self.build_virtual_machine()

		virtual_machine.set_termination_protection("true")
		self.assertTrue(virtual_machine.is_termination_protected)

		virtual_machine.set_termination_protection(False)
		self.assertFalse(virtual_machine.is_termination_protected)
		self.assertEqual(virtual_machine.save.call_count, 2)

	def test_a_terminating_virtual_machine_rejects_the_change(self) -> None:
		virtual_machine = self.build_virtual_machine()
		virtual_machine.is_terminating = 1

		with self.assertRaises(AtlasUserError):
			virtual_machine.set_termination_protection(True)


class TestVirtualMachinePrivilege(UnitTestCase):
	"""Cover the Atlas WG Mesh privilege flag."""

	def build_virtual_machine(self, *, tenant_id: int, is_privileged: int = 0) -> VirtualMachine:
		virtual_machine = VirtualMachine.__new__(VirtualMachine)
		virtual_machine.name = "VM-00001"
		virtual_machine.tenant_id = tenant_id
		virtual_machine.is_privileged = is_privileged
		virtual_machine.is_draft = 0
		virtual_machine.is_terminating = 0
		virtual_machine.active_migration = None
		virtual_machine.check_permission = Mock()
		virtual_machine.save = Mock()
		return virtual_machine

	def test_set_privileged_reads_the_boolean(self) -> None:
		virtual_machine = self.build_virtual_machine(tenant_id=0)

		virtual_machine.set_privileged("true")

		self.assertTrue(virtual_machine.is_privileged)
		virtual_machine.save.assert_called_once()

	def test_set_privileged_revokes(self) -> None:
		virtual_machine = self.build_virtual_machine(tenant_id=0, is_privileged=1)

		with patch.object(virtual_machine_module.frappe, "only_for"):
			virtual_machine.set_privileged(False)

		self.assertFalse(virtual_machine.is_privileged)

	def test_set_privileged_rejects_a_draft(self) -> None:
		virtual_machine = self.build_virtual_machine(tenant_id=0)
		virtual_machine.is_draft = 1

		with (
			patch.object(virtual_machine_module.frappe, "only_for"),
			self.assertRaises(frappe.ValidationError),
		):
			virtual_machine.set_privileged(True)

		virtual_machine.save.assert_not_called()

	def test_validate_refuses_privilege_outside_tenant_zero(self) -> None:
		virtual_machine = self.build_virtual_machine(tenant_id=5, is_privileged=1)

		with self.assertRaises(frappe.ValidationError):
			virtual_machine.validate()

	def test_validate_allows_tenant_zero_without_privilege(self) -> None:
		"""Tenant 0 alone is not privileged, so a plain tenant-0 VM is valid."""
		self.build_virtual_machine(tenant_id=0).validate()

	def test_create_accepts_a_privileged_vm_of_tenant_zero(self) -> None:
		request = VirtualMachineCreateRequest.from_value(
			{
				"virtual_machine_image": "image-1",
				"cpu_millicores": 2000,
				"memory_mib": 1024,
				"disk_mib": 1024,
				"tenant_id": 0,
				"is_privileged": True,
			}
		)

		with (
			patch.object(virtual_machine_module.frappe, "has_permission", return_value=True),
			patch.object(
				VirtualMachineService, "create", return_value={"name": "VM-00001", "is_draft": False}
			) as create,
		):
			virtual_machine_module.create(request)

		self.assertTrue(create.call_args.args[0].is_privileged)


class TestSystemImageCreation(UnitTestCase):
	"""Only tenant 0 may share an image or set the host image flags."""

	def create_image(self, *, tenant_id: int, **options):
		"""Run one snapshot request for one tenant."""
		virtual_machine = VirtualMachine.__new__(VirtualMachine)
		virtual_machine.name = "VM-00001"
		virtual_machine.tenant_id = tenant_id
		virtual_machine.is_draft = 0
		virtual_machine.is_terminating = 0
		virtual_machine.active_migration = None
		virtual_machine.check_permission = Mock()
		with patch(
			"atlas.vm.core.vm_image_transfer.VirtualMachineImageTransferService.create_from_virtual_machine",
			return_value="IMG-00001",
		) as create:
			name = virtual_machine.create_machine_image("golden", **options)
		return name, create

	def test_tenant_zero_creates_a_system_image(self) -> None:
		name, create = self.create_image(
			tenant_id=0, image_type="system", cache_image=True, memory_snapshot=True
		)

		self.assertEqual(name, "IMG-00001")
		self.assertEqual(create.call_args.kwargs["image_type"], "system")
		self.assertTrue(create.call_args.kwargs["cache_image"])
		self.assertTrue(create.call_args.kwargs["memory_snapshot"])

	def test_another_tenant_creates_a_machine_image(self) -> None:
		name, create = self.create_image(tenant_id=7)

		self.assertEqual(name, "IMG-00001")
		self.assertEqual(create.call_args.kwargs["image_type"], "machine")

	def test_a_shape_without_a_memory_snapshot_is_rejected(self) -> None:
		with self.assertRaises(AtlasUserError):
			self.create_image(tenant_id=0, memory_snapshot_configuration={"memory_mib": 4096})

	def test_another_tenant_cannot_use_a_shared_option(self) -> None:
		for options in ({"image_type": "system"}, {"cache_image": True}, {"memory_snapshot": True}):
			with self.assertRaises(AtlasUserError):
				self.create_image(tenant_id=7, **options)
