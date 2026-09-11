from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests import UnitTestCase

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.vm.core import vm_service as virtual_machine_service_module
from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.metal_models import MetalVirtualMachine
from atlas.vm.core.models import VirtualMachineCreateRequest
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
			"virtual_cpu_count": 2,
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


COMPUTE_REQUEST = {
	"virtual_cpu_count": 2,
	"memory_mib": 2048,
	"sleep_after_idle_seconds": 1800,
}


def metal_virtual_machine_response(status_code: int = 200) -> SimpleNamespace:
	return SimpleNamespace(
		status_code=status_code,
		content=b"{}",
		json=lambda: METAL_VIRTUAL_MACHINE_RESPONSE,
	)


class TestVirtualMachineRequest(UnitTestCase):
	def test_request_parses_ssh_keys_and_defaults(self) -> None:
		request = VirtualMachineCreateRequest.from_value(
			{
				"virtual_machine_image": "Ubuntu 24.04",
				"vcpus": 2,
				"memory_mib": 2048,
				"disk_mib": 10240,
				"tenant_id": 7,
				"ssh_keys": "key-one\nkey-two",
			}
		)

		self.assertEqual(request.ssh_keys, ("key-one", "key-two"))
		self.assertEqual(request.egress, "uplink")

	def test_request_parses_metadata(self) -> None:
		request = VirtualMachineCreateRequest.from_value(
			{
				"virtual_machine_image": "Ubuntu 24.04",
				"vcpus": 2,
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
					"vcpus": 2,
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
				"vcpus": 2,
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
				"vcpus": 2,
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
				"vcpus": 2,
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
			"vcpus": 2,
			"memory_mib": 2048,
			"disk_mib": 10240,
			"tenant_id": 7,
			"egress": "mesh",
		}
		with self.assertRaises(ValueError):
			VirtualMachineCreateRequest.from_value({**base, "server_ip_address": "203.0.113.10"})

		# A public limit is stored, not rejected, so a mode change needs no cleanup.
		request = VirtualMachineCreateRequest.from_value({**base, "public_network_throughput_mibps": 50})
		self.assertEqual(request.public_network_throughput_mibps, 50)

	def test_request_rejects_negative_throughput(self) -> None:
		with self.assertRaises(ValueError):
			VirtualMachineCreateRequest.from_value(
				{
					"virtual_machine_image": "Ubuntu 24.04",
					"vcpus": 2,
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
					"vcpus": True,
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
					"vcpus": 2,
					"memory_mib": 2048,
					"disk_mib": 10240,
					"tenant_id": True,
				}
			)


class TestVirtualMachineDocument(UnitTestCase):
	# Frappe reads every virtual field to build the new document template. A new
	# record has no Server, so the Metal lookup must not run.
	def test_new_document_reads_virtual_fields_without_a_server(self) -> None:
		virtual_machine = frappe.new_doc("Virtual Machine")

		self.assertIsNone(virtual_machine.get_metal_vm_info())
		self.assertEqual(virtual_machine.current_state, "unknown")
		self.assertIsNone(virtual_machine.desired_state)


class TestVirtualMachineService(UnitTestCase):
	def test_machine_image_uses_its_own_artifacts(self) -> None:
		request = VirtualMachineCreateRequest("machine-image", 2, 2048, 10240, 7)
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
		image.get_metal_image_request.assert_called_once_with("")

	def test_metal_request_carries_throughput_limits(self) -> None:
		request = VirtualMachineCreateRequest(
			"machine-image",
			2,
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
				"virtual_cpu_count": 2,
				"memory_mib": 2048,
				"sleep_after_idle_seconds": 0,
			},
		)
		self.assertEqual(metal_request["disk"]["size_mib"], 10240)
		self.assertEqual(metal_request["guest"]["ssh_keys"], [])
		self.assertEqual(metal_request["network"]["private_network_throughput_mibps"], 100)
		self.assertEqual(metal_request["network"]["public_network_throughput_mibps"], 50)


class TestMetalClient(UnitTestCase):
	@patch("atlas.vm.core.metal_client.get_decrypted_password", return_value="token")
	def test_client_uses_the_validated_public_ipv4_address(self, _get_password: Mock) -> None:
		client = MetalClient(SimpleNamespace(name="Server-1", public_ipv4_address="203.0.113.8"))

		self.assertEqual(client.base_url, "http://203.0.113.8:9000")

	@patch("atlas.vm.core.metal_client.get_decrypted_password")
	def test_client_rejects_an_invalid_public_ipv4_address(self, get_password: Mock) -> None:
		with self.assertRaisesRegex(MetalClientError, "invalid public IPv4 address"):
			MetalClient(SimpleNamespace(name="Server-1", public_ipv4_address="not-an-address"))

		get_password.assert_not_called()

	def test_error_keeps_contract_fields(self) -> None:
		error = MetalClientError("busy", status=503, code="host_busy", retryable=True, uncertain=True)

		self.assertEqual(error.status, 503)
		self.assertEqual(error.code, "host_busy")
		self.assertTrue(error.retryable)
		self.assertTrue(error.uncertain)
		self.assertFalse(error.is_not_found)

	def test_put_uses_the_atlas_vm_name(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = metal_virtual_machine_response(202)

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			client.put_virtual_machine("VM-00001", {"vcpus": 1})

		self.assertEqual(request.call_args.args[:2], ("PUT", "http://10.0.0.2:9000/v1/vms/VM-00001"))

	def test_client_uses_versioned_mutation_paths(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = metal_virtual_machine_response(202)

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			client.set_virtual_machine_power_state("VM-00001", "running")
			client.request_virtual_machine_restart("VM-00001")
			client.set_virtual_machine_disk("VM-00001", {"size_mib": 2048, "throughput_mibps": 0, "iops": 0})
			client.set_virtual_machine_compute("VM-00001", COMPUTE_REQUEST)
			client.delete_virtual_machine("VM-00001")

		paths = [call.args[1] for call in request.call_args_list]
		self.assertEqual(
			paths,
			[
				"http://10.0.0.2:9000/v1/vms/VM-00001/power",
				"http://10.0.0.2:9000/v1/vms/VM-00001/restart",
				"http://10.0.0.2:9000/v1/vms/VM-00001/disk",
				"http://10.0.0.2:9000/v1/vms/VM-00001/compute",
				"http://10.0.0.2:9000/v1/vms/VM-00001",
			],
		)
		self.assertEqual(
			request.call_args_list[2].kwargs["json"],
			{"size_mib": 2048, "throughput_mibps": 0, "iops": 0},
		)
		self.assertEqual(request.call_args_list[3].kwargs["json"], COMPUTE_REQUEST)

	def test_console_connection_builds_websocket_url(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}

		connection = client.get_console_connection("VM-00001")
		ssh_connection = client.get_console_connection("VM-00001", "ssh")

		self.assertEqual(connection["url"], "ws://10.0.0.2:9000/v1/vms/VM-00001/console?mode=tty")
		self.assertEqual(ssh_connection["url"], "ws://10.0.0.2:9000/v1/vms/VM-00001/console?mode=ssh")
		self.assertEqual(connection["authorization"], "Bearer token")

	def test_replace_ssh_keys_uses_vm_subresource(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = metal_virtual_machine_response()

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			client.replace_virtual_machine_ssh_keys("VM-00001", ["ssh-ed25519 AAAA"])

		self.assertEqual(
			request.call_args.args[:2],
			("PUT", "http://10.0.0.2:9000/v1/vms/VM-00001/ssh-keys"),
		)
		self.assertEqual(request.call_args.kwargs["json"], {"ssh_keys": ["ssh-ed25519 AAAA"]})

	def test_replace_metadata_uses_vm_subresource(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = metal_virtual_machine_response()

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			client.replace_virtual_machine_metadata("VM-00001", {"env": "prod"})

		self.assertEqual(
			request.call_args.args[:2],
			("PUT", "http://10.0.0.2:9000/v1/vms/VM-00001/metadata"),
		)
		self.assertEqual(request.call_args.kwargs["json"], {"metadata": {"env": "prod"}})

	def test_set_disk_uses_vm_subresource(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = metal_virtual_machine_response(202)
		disk = {"size_mib": 2048, "throughput_mibps": 50, "iops": 2000}

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			client.set_virtual_machine_disk("VM-00001", disk)

		self.assertEqual(
			request.call_args.args[:2],
			("PUT", "http://10.0.0.2:9000/v1/vms/VM-00001/disk"),
		)
		self.assertEqual(request.call_args.kwargs["json"], disk)

	def test_set_network_uses_vm_subresource(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = metal_virtual_machine_response(202)
		network = {
			"egress": "uplink",
			"public_ipv4": "203.0.113.10",
			"wireguard_mesh_ipv6": "fdaa:1::1",
			"private_network_throughput_mibps": 100,
			"public_network_throughput_mibps": 50,
		}

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			client.set_virtual_machine_network("VM-00001", network)

		self.assertEqual(
			request.call_args.args[:2],
			("PUT", "http://10.0.0.2:9000/v1/vms/VM-00001/network"),
		)
		self.assertEqual(request.call_args.kwargs["json"], network)

	def test_snapshot_calls_use_unified_image_paths(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		responses = [
			SimpleNamespace(status_code=201, content=b"{}", json=lambda: {}),
			SimpleNamespace(status_code=202, content=b""),
			SimpleNamespace(
				status_code=200, content=b'{"state": "uploading"}', json=lambda: {"state": "uploading"}
			),
			SimpleNamespace(status_code=204, content=b""),
		]

		with patch("atlas.vm.core.metal_client.requests.request", side_effect=responses) as request:
			client.create_snapshot("VM-00001")
			client.start_snapshot_upload("image-1", {"rootfs": {"parts": []}, "kernel": {"parts": []}})
			client.get_snapshot("image-1")
			client.delete_snapshot("image-1")

		self.assertEqual(
			[call.args[:2] for call in request.call_args_list],
			[
				("POST", "http://10.0.0.2:9000/v1/vms/VM-00001/snapshots"),
				("POST", "http://10.0.0.2:9000/v1/snapshots/image-1/upload"),
				("GET", "http://10.0.0.2:9000/v1/snapshots/image-1"),
				("DELETE", "http://10.0.0.2:9000/v1/snapshots/image-1"),
			],
		)
		self.assertEqual(request.call_args_list[0].kwargs["timeout"], client.snapshot_timeout_seconds)
		self.assertEqual(request.call_args_list[1].kwargs["timeout"], client.snapshot_timeout_seconds)

	def test_sync_sends_wireguard_peers_images_and_privileged_addresses(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = SimpleNamespace(status_code=200, content=b"{}", json=lambda: {"capacity": {}})

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			result = client.sync([{"node": "node-1"}], [{"ref": "sha256:image"}], ["fdaa:1::1"])

		self.assertEqual(result, {"capacity": {}})
		self.assertEqual(request.call_args.args[:2], ("POST", "http://10.0.0.2:9000/v1/sync"))
		self.assertEqual(
			request.call_args.kwargs["json"],
			{
				"wireguard_peers": [{"node": "node-1"}],
				"images": [{"ref": "sha256:image"}],
				"privileged_vm_addresses": ["fdaa:1::1"],
			},
		)

	def test_snapshot_transport_error_is_uncertain(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {}

		with (
			patch(
				"atlas.vm.core.metal_client.requests.request", side_effect=requests.ConnectionError("lost")
			),
			self.assertRaises(MetalClientError) as raised,
		):
			client.start_snapshot_upload("image-1", {})

		self.assertTrue(raised.exception.uncertain)

	def test_transport_error_marks_virtual_machine_writes_as_uncertain(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {}

		write_operations = {
			"create": lambda: client.put_virtual_machine("VM-00001", {}),
			"restart": lambda: client.request_virtual_machine_restart("VM-00001"),
			"delete": lambda: client.delete_virtual_machine("VM-00001"),
			"power": lambda: client.set_virtual_machine_power_state("VM-00001", "running"),
			"ssh_keys": lambda: client.replace_virtual_machine_ssh_keys("VM-00001", []),
			"metadata": lambda: client.replace_virtual_machine_metadata("VM-00001", {}),
			"network": lambda: client.set_virtual_machine_network("VM-00001", {}),
			"disk": lambda: client.set_virtual_machine_disk("VM-00001", {}),
			"compute": lambda: client.set_virtual_machine_compute("VM-00001", COMPUTE_REQUEST),
			"sync": lambda: client.sync([], [], []),
		}

		for operation_name, write_operation in write_operations.items():
			with (
				self.subTest(operation=operation_name),
				patch(
					"atlas.vm.core.metal_client.requests.request",
					side_effect=requests.ConnectionError("lost"),
				),
				self.assertRaises(MetalClientError) as raised,
			):
				write_operation()

			self.assertTrue(raised.exception.uncertain)

	def test_error_uses_the_metal_retryable_value(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {}
		response = SimpleNamespace(
			status_code=500,
			content=b"{}",
			json=lambda: {"error": {"code": "internal_error", "message": "failed", "retryable": False}},
		)

		with (
			patch("atlas.vm.core.metal_client.requests.request", return_value=response),
			self.assertRaises(MetalClientError) as raised,
		):
			client.get_virtual_machine("VM-00001")

		self.assertFalse(raised.exception.retryable)


class TestMetalVirtualMachineModel(UnitTestCase):
	def test_model_parses_nested_desired_and_observed_state(self) -> None:
		information = MetalVirtualMachine.from_dict(METAL_VIRTUAL_MACHINE_RESPONSE)

		self.assertEqual(information.desired.compute.virtual_cpu_count, 2)
		self.assertEqual(information.desired.disk.size_mib, 2048)
		self.assertEqual(information.desired.network.wireguard_mesh_ipv6, "fdaa:1::1")
		self.assertEqual(information.observed.disk.used_mib, 1024)
		self.assertEqual(information.observed.network.mac, "06:00:00:00:00:01")

	def test_model_rejects_the_old_flat_response(self) -> None:
		with self.assertRaises(ValueError):
			MetalVirtualMachine.from_dict({"id": "VM-00001", "state": "running"})

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
		self.assertEqual(virtual_machine.public_ipv4, "203.0.113.10")
		self.assertEqual(virtual_machine.disk_throughput_mibps, 50)
		self.assertEqual(virtual_machine.disk_iops, 2000)
		self.assertEqual(virtual_machine.private_network_throughput_mibps, 100)
		self.assertEqual(virtual_machine.public_network_throughput_mibps, 50)
		self.assertEqual(virtual_machine.ssh_keys, "ssh-ed25519 AAAA")
		self.assertEqual(virtual_machine.metadata, '{\n  "env": "prod"\n}')


class TestVirtualMachineNetwork(UnitTestCase):
	"""Cover the live network updates that do not restart the VM."""

	def build_virtual_machine(self, network: dict) -> tuple[VirtualMachine, Mock]:
		virtual_machine = VirtualMachine.__new__(VirtualMachine)
		virtual_machine.name = "VM-00001"
		virtual_machine.server = "node-1"
		virtual_machine.is_draft = 0
		virtual_machine.is_terminating = 0

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
			},
		)

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

	def test_attach_ip_address_requests_host_egress(self) -> None:
		virtual_machine, client = self.build_virtual_machine({"egress": "none"})
		address = SimpleNamespace(address="203.0.113.10")
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
		self.assertEqual(request["public_ipv4"], "203.0.113.10")

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

	def test_trash_removes_dependent_records(self) -> None:
		"""Dependent records must not prevent the virtual machine deletion."""
		virtual_machine = Mock(doctype="Virtual Machine")
		virtual_machine.name = "vm-00003"

		with (
			patch.object(virtual_machine_module, "VirtualMachineService") as service,
			patch.object(virtual_machine_module, "delete_tasks_for_target") as delete_tasks,
			patch.object(virtual_machine_module.frappe.db, "delete") as delete,
		):
			virtual_machine_module.VirtualMachine.on_trash(virtual_machine)

		service.return_value.validate_deletion.assert_called_once()
		delete_tasks.assert_called_once_with("Virtual Machine", "vm-00003")
		delete.assert_called_once_with("Virtual Machine State", {"name": "vm-00003"})


class TestReconcileTerminating(UnitTestCase):
	"""Cover the scheduled cleanup of terminated VMs."""

	def _run(self, error: MetalClientError | None) -> Mock:
		virtual_machine = Mock(server="node-1", flags=SimpleNamespace())
		client = Mock()
		if error is not None:
			client.get_virtual_machine.side_effect = error
		with (
			patch.object(
				virtual_machine_module.frappe,
				"get_doc",
				side_effect=[virtual_machine, Mock()],
			),
			patch.object(virtual_machine_service_module, "MetalClient", return_value=client),
		):
			virtual_machine_module.reconcile_terminating_virtual_machine("VM-00001")
		return virtual_machine

	def test_absent_vm_is_deleted(self) -> None:
		virtual_machine = self._run(MetalClientError("gone", status=404))
		self.assertTrue(virtual_machine.flags.metal_absence_confirmed)
		virtual_machine.delete.assert_called_once()

	def test_present_vm_is_kept(self) -> None:
		virtual_machine = self._run(None)
		virtual_machine.delete.assert_not_called()

	def test_other_error_keeps_vm(self) -> None:
		with patch.object(virtual_machine_service_module.frappe, "log_error") as log_error:
			virtual_machine = self._run(MetalClientError("busy", status=503))

		virtual_machine.delete.assert_not_called()
		self.assertEqual(
			log_error.call_args.kwargs["title"],
			"Virtual Machine VM-00001 termination reconciliation failed",
		)


class TestVirtualMachinePrivilege(UnitTestCase):
	"""Cover the Atlas WG Mesh privilege flag."""

	def build_virtual_machine(self, *, tenant_id: int, is_privileged: int = 0) -> VirtualMachine:
		virtual_machine = VirtualMachine.__new__(VirtualMachine)
		virtual_machine.name = "VM-00001"
		virtual_machine.tenant_id = tenant_id
		virtual_machine.is_privileged = is_privileged
		virtual_machine.is_draft = 0
		virtual_machine.is_terminating = 0
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
				"vcpus": 2,
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

	def test_another_tenant_cannot_use_a_shared_option(self) -> None:
		for options in ({"image_type": "system"}, {"cache_image": True}, {"memory_snapshot": True}):
			with self.assertRaises(AtlasUserError):
				self.create_image(tenant_id=7, **options)
