from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests import UnitTestCase

from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.virtual_machine_manager import (
	PlacementCapacity,
	VirtualMachineCreateRequest,
	VirtualMachineManager,
)
from atlas.vm.doctype.virtual_machine import virtual_machine as virtual_machine_module
from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine


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

	def test_placement_checks_capacity_and_architecture(self) -> None:
		request = VirtualMachineCreateRequest("image", 2, 2048, 10240, 7)
		capacity = PlacementCapacity("node-1", "amd64", 4, 4096, 20480)

		self.assertTrue(capacity.can_host(request, "amd64"))
		self.assertFalse(capacity.can_host(request, "arm64"))


class TestVirtualMachineManager(UnitTestCase):
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

		with patch.object(VirtualMachineManager, "get_wireguard_mesh_ipv6", return_value="fdaa::1"):
			metal_request = VirtualMachineManager().get_metal_request(request, image, virtual_machine, None)

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

		with patch.object(VirtualMachineManager, "get_wireguard_mesh_ipv6", return_value="fdaa::1"):
			metal_request = VirtualMachineManager().get_metal_request(request, image, virtual_machine, None)

		self.assertEqual(metal_request["network"]["private_network_throughput_mibps"], 100)
		self.assertEqual(metal_request["network"]["public_network_throughput_mibps"], 50)


class TestMetalClient(UnitTestCase):
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
		response = SimpleNamespace(status_code=202, content=b"")

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			client.put_virtual_machine("VM-00001", {"vcpus": 1})

		self.assertEqual(request.call_args.args[:2], ("PUT", "http://10.0.0.2:9000/vms/VM-00001"))

	def test_client_uses_resource_action_and_sync_paths(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = SimpleNamespace(status_code=202, content=b"")

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			client.perform_action("VM-00001", "start")
			client.resize_virtual_machine_disk("VM-00001", 2048)
			client.resize_virtual_machine_compute("VM-00001", 2, 2048)
			client.terminate_virtual_machine("VM-00001")

		paths = [call.args[1] for call in request.call_args_list]
		self.assertEqual(
			paths,
			[
				"http://10.0.0.2:9000/vms/VM-00001/actions/start",
				"http://10.0.0.2:9000/vms/VM-00001/resize/disk",
				"http://10.0.0.2:9000/vms/VM-00001/resize/compute",
				"http://10.0.0.2:9000/vms/VM-00001/actions/terminate",
			],
		)
		self.assertEqual(request.call_args_list[2].kwargs["json"], {"vcpus": 2, "memory_mib": 2048})

	def test_console_connection_builds_websocket_url(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}

		connection = client.get_console_connection("VM-00001")
		ssh_connection = client.get_console_connection("VM-00001", "ssh")

		self.assertEqual(connection["url"], "ws://10.0.0.2:9000/vms/VM-00001/console?mode=tty")
		self.assertEqual(ssh_connection["url"], "ws://10.0.0.2:9000/vms/VM-00001/console?mode=ssh")
		self.assertEqual(connection["authorization"], "Bearer token")

	def test_replace_ssh_keys_uses_vm_subresource(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = SimpleNamespace(status_code=200, content=b"{}", json=lambda: {})

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			client.replace_virtual_machine_ssh_keys("VM-00001", ["ssh-ed25519 AAAA"])

		self.assertEqual(
			request.call_args.args[:2],
			("PUT", "http://10.0.0.2:9000/vms/VM-00001/ssh-keys"),
		)
		self.assertEqual(request.call_args.kwargs["json"], {"ssh_keys": ["ssh-ed25519 AAAA"]})

	def test_replace_metadata_uses_vm_subresource(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = SimpleNamespace(status_code=200, content=b"{}", json=lambda: {})

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			client.replace_virtual_machine_metadata("VM-00001", {"env": "prod"})

		self.assertEqual(
			request.call_args.args[:2],
			("PUT", "http://10.0.0.2:9000/vms/VM-00001/metadata"),
		)
		self.assertEqual(request.call_args.kwargs["json"], {"metadata": {"env": "prod"}})

	def test_update_disk_uses_vm_subresource(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = SimpleNamespace(status_code=200, content=b"{}", json=lambda: {})
		disk = {"throughput_mibps": 50, "iops": 2000}

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			client.update_virtual_machine_disk("VM-00001", disk)

		self.assertEqual(
			request.call_args.args[:2],
			("PUT", "http://10.0.0.2:9000/vms/VM-00001/disk"),
		)
		self.assertEqual(request.call_args.kwargs["json"], disk)

	def test_update_network_uses_vm_subresource(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = SimpleNamespace(status_code=200, content=b"{}", json=lambda: {})
		network = {
			"egress": "uplink",
			"public_ipv4": "203.0.113.10",
			"private_network_throughput_mibps": 100,
			"public_network_throughput_mibps": 50,
		}

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			client.update_virtual_machine_network("VM-00001", network)

		self.assertEqual(
			request.call_args.args[:2],
			("PUT", "http://10.0.0.2:9000/vms/VM-00001/network"),
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
				("POST", "http://10.0.0.2:9000/vms/VM-00001/snapshots"),
				("POST", "http://10.0.0.2:9000/snapshots/image-1/upload"),
				("GET", "http://10.0.0.2:9000/snapshots/image-1"),
				("DELETE", "http://10.0.0.2:9000/snapshots/image-1"),
			],
		)
		self.assertEqual(request.call_args_list[0].kwargs["timeout"], client.snapshot_timeout_seconds)
		self.assertEqual(request.call_args_list[1].kwargs["timeout"], client.snapshot_timeout_seconds)

	def test_sync_sends_wireguard_peers_and_images(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {"Authorization": "Bearer token"}
		response = SimpleNamespace(status_code=200, content=b"{}", json=lambda: {"capacity": {}})

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response) as request:
			result = client.sync([{"node": "node-1"}], [{"ref": "sha256:image"}], ["fdaa:1::1"])

		self.assertEqual(result, {"capacity": {}})
		self.assertEqual(request.call_args.args[:2], ("POST", "http://10.0.0.2:9000/sync"))
		self.assertEqual(
			request.call_args.kwargs["json"],
			{"wireguard_peers": [{"node": "node-1"}], "images": [{"ref": "sha256:image"}]},
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

	def test_transport_error_marks_put_as_uncertain(self) -> None:
		client = MetalClient.__new__(MetalClient)
		client.base_url = "http://10.0.0.2:9000"
		client.headers = {}

		with (
			patch(
				"atlas.vm.core.metal_client.requests.request", side_effect=requests.ConnectionError("lost")
			),
			self.assertRaises(MetalClientError) as raised,
		):
			client.put_virtual_machine("VM-00001", {})

		self.assertTrue(raised.exception.uncertain)


class TestVirtualMachineNetwork(UnitTestCase):
	"""Cover the live network updates that do not restart the VM."""

	def build_virtual_machine(self, network: dict) -> tuple[VirtualMachine, Mock]:
		virtual_machine = VirtualMachine.__new__(VirtualMachine)
		virtual_machine.name = "VM-00001"
		virtual_machine.server = "node-1"
		virtual_machine.is_draft = 0
		virtual_machine.is_terminating = 0

		client = Mock()
		client.get_virtual_machine.return_value = {"network": network}
		client.update_virtual_machine_network.return_value = {"network": network}
		return virtual_machine, client

	def patches(self, client: Mock, address_name: str | None) -> tuple[Any, ...]:
		return (
			patch.object(virtual_machine_module, "MetalClient", return_value=client),
			patch.object(virtual_machine_module.frappe, "get_doc", return_value=Mock()),
			patch.object(virtual_machine_module.frappe, "only_for"),
			patch.object(
				virtual_machine_module.frappe,
				"db",
				Mock(exists=Mock(return_value=address_name)),
			),
		)

	def test_update_network_keeps_the_unchanged_metal_values(self) -> None:
		virtual_machine, client = self.build_virtual_machine(
			{
				"egress": "uplink",
				"public_ipv4": "203.0.113.10",
				"private_network_throughput_mibps": 100,
				"public_network_throughput_mibps": 50,
			}
		)

		with (
			patch.object(virtual_machine_module, "MetalClient", return_value=client),
			patch.object(virtual_machine_module.frappe, "get_doc", return_value=Mock()),
		):
			virtual_machine.update_network(public_network_throughput_mibps=25)

		client.update_virtual_machine_network.assert_called_once_with(
			"VM-00001",
			{
				"egress": "uplink",
				"public_ipv4": "203.0.113.10",
				"private_network_throughput_mibps": 100,
				"public_network_throughput_mibps": 25,
			},
		)

	def test_update_network_stops_when_metal_reports_no_settings(self) -> None:
		"""An unknown current state must not detach the address through a default."""
		virtual_machine, client = self.build_virtual_machine({})

		with (
			patch.object(virtual_machine_module, "MetalClient", return_value=client),
			patch.object(virtual_machine_module.frappe, "get_doc", return_value=Mock()),
			self.assertRaises(frappe.ValidationError),
		):
			virtual_machine.update_network(public_network_throughput_mibps=25)

		client.update_virtual_machine_network.assert_not_called()

	def test_update_network_rejects_a_draft(self) -> None:
		virtual_machine, client = self.build_virtual_machine({"egress": "uplink"})
		virtual_machine.is_draft = 1

		with (
			patch.object(virtual_machine_module, "MetalClient", return_value=client),
			self.assertRaises(frappe.ValidationError),
		):
			virtual_machine.update_network(public_network_throughput_mibps=25)

	def test_attach_ip_address_requests_host_egress(self) -> None:
		virtual_machine, client = self.build_virtual_machine({"egress": "none"})
		address = SimpleNamespace(address="203.0.113.10")
		metal_client, get_doc, only_for, database = self.patches(client, None)

		with (
			metal_client,
			get_doc,
			only_for,
			database,
			patch.object(VirtualMachine, "assign_ip_address", return_value=address) as assign,
		):
			virtual_machine.attach_ip_address("203.0.113.10")

		assign.assert_called_once_with("203.0.113.10")
		request = client.update_virtual_machine_network.call_args.args[1]
		self.assertEqual(request["egress"], "uplink")
		self.assertEqual(request["public_ipv4"], "203.0.113.10")

	def test_attach_ip_address_rejects_a_second_address(self) -> None:
		virtual_machine, client = self.build_virtual_machine({"egress": "uplink"})
		metal_client, get_doc, only_for, database = self.patches(client, "203.0.113.10")

		with metal_client, get_doc, only_for, database, self.assertRaises(frappe.ValidationError):
			virtual_machine.attach_ip_address("203.0.113.11")

		client.update_virtual_machine_network.assert_not_called()

	def test_detach_ip_address_updates_metal_before_the_release(self) -> None:
		virtual_machine, client = self.build_virtual_machine(
			{"egress": "uplink", "public_ipv4": "203.0.113.10"}
		)
		calls: list[str] = []
		client.update_virtual_machine_network.side_effect = lambda *arguments: calls.append("metal") or {}
		metal_client, get_doc, only_for, database = self.patches(client, "203.0.113.10")

		with (
			metal_client,
			get_doc,
			only_for,
			database,
			patch.object(
				VirtualMachine,
				"release_ip_address",
				side_effect=lambda self=None: calls.append("release"),
			),
		):
			virtual_machine.detach_ip_address()

		self.assertEqual(calls, ["metal", "release"])
		request = client.update_virtual_machine_network.call_args.args[1]
		self.assertEqual(request["public_ipv4"], "")
		self.assertEqual(request["egress"], "uplink")

	def test_update_egress_sends_the_new_mode(self) -> None:
		virtual_machine, client = self.build_virtual_machine({"egress": "uplink"})
		metal_client, get_doc, only_for, database = self.patches(client, None)

		with metal_client, get_doc, only_for, database:
			virtual_machine.update_egress("mesh")

		self.assertEqual(client.update_virtual_machine_network.call_args.args[1]["egress"], "mesh")

	def test_update_egress_rejects_an_unknown_mode(self) -> None:
		virtual_machine, client = self.build_virtual_machine({"egress": "uplink"})
		metal_client, get_doc, only_for, database = self.patches(client, None)

		with metal_client, get_doc, only_for, database, self.assertRaises(frappe.ValidationError):
			virtual_machine.update_egress("server")

		client.update_virtual_machine_network.assert_not_called()

	def test_update_egress_keeps_the_internet_path_for_an_attached_address(self) -> None:
		"""A public IPv4 address needs uplink, so Atlas refuses mesh and none."""
		virtual_machine, client = self.build_virtual_machine(
			{"egress": "uplink", "public_ipv4": "203.0.113.10"}
		)

		for egress in ("mesh", "none"):
			metal_client, get_doc, only_for, database = self.patches(client, "203.0.113.10")
			with metal_client, get_doc, only_for, database, self.assertRaises(frappe.ValidationError):
				virtual_machine.update_egress(egress)

		client.update_virtual_machine_network.assert_not_called()

	def test_update_network_throughput_rejects_bad_values(self) -> None:
		"""A malformed value must fail, not silently become 0 and remove the limit."""
		virtual_machine, client = self.build_virtual_machine({"egress": "uplink"})

		for private, public in ((-1, 0), ("abc", 0), (0, "")):
			metal_client, get_doc, only_for, database = self.patches(client, None)
			with metal_client, get_doc, only_for, database, self.assertRaises(frappe.ValidationError):
				virtual_machine.update_network_throughput(private, public)

		client.update_virtual_machine_network.assert_not_called()

	def test_update_egress_keeps_a_stored_public_limit(self) -> None:
		"""Atlas resends every setting, so a stored public limit must not block a mode change."""
		virtual_machine, client = self.build_virtual_machine(
			{"egress": "uplink", "public_network_throughput_mibps": 31}
		)
		metal_client, get_doc, only_for, database = self.patches(client, None)

		with metal_client, get_doc, only_for, database:
			virtual_machine.update_egress("mesh")

		request = client.update_virtual_machine_network.call_args.args[1]
		self.assertEqual(request["egress"], "mesh")
		self.assertEqual(request["public_network_throughput_mibps"], 31)


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
			patch.object(virtual_machine_module, "MetalClient", return_value=client),
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
		virtual_machine = self._run(MetalClientError("busy", status=503))
		virtual_machine.delete.assert_not_called()
