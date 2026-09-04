from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from frappe.tests import UnitTestCase

from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.virtual_machine_manager import (
	PlacementCapacity,
	VirtualMachineCreateRequest,
	VirtualMachineManager,
)
from atlas.vm.doctype.virtual_machine import virtual_machine as virtual_machine_module


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
		self.assertEqual(request.egress, "host")

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
			result = client.sync([{"node": "node-1"}], [{"ref": "sha256:image"}])

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
