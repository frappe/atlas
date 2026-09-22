from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import requests
from frappe.tests import UnitTestCase

from atlas.vm.core.metal_client import MetalClient, MetalClientError


def build_client() -> MetalClient:
	client = MetalClient.__new__(MetalClient)
	client.base_url = "https://10.0.0.2:9000"
	client.ca_file = "/private/atlas-metal-tls/ca.crt"
	client.client_certificate = ("/private/atlas-metal-tls/atlas.crt", "/private/atlas-metal-tls/atlas.key")
	client.timeout_seconds = 5
	client.retry_delay_seconds = 0
	return client


def build_response(status: int, body: Any = None, content: bytes | None = None) -> Mock:
	response = Mock(spec=requests.Response)
	response.status_code = status
	response.content = content if content is not None else (b"{}" if body is None else b"body")
	response.json.return_value = {} if body is None else body
	return response


class TestMetalClientRetries(UnitTestCase):
	def test_a_retryable_failure_is_repeated_until_it_succeeds(self) -> None:
		client = build_client()
		responses = [requests.ReadTimeout("read timed out"), build_response(200, {"state": "running"})]

		with patch("atlas.vm.core.metal_client.requests.Session.request", side_effect=responses) as request:
			body = client._request("GET", "/v1/vms/VM-00001", attempts=client.status_attempts)

		self.assertEqual(body, {"state": "running"})
		self.assertEqual(request.call_count, 2)

	def test_retries_share_one_deadline(self) -> None:
		"""Three attempts must not cost three full read timeouts."""
		client = build_client()
		clock = iter(range(0, 400))
		seen: list[tuple[float, float]] = []

		def record(*_args, **kwargs):
			seen.append(kwargs["timeout"])
			raise requests.ReadTimeout("read timed out")

		with (
			patch("atlas.vm.core.metal_client.monotonic", lambda: next(clock) * 10),
			patch("atlas.vm.core.metal_client.requests.Session.request", side_effect=record),
		):
			with self.assertRaises(MetalClientError):
				client._request("GET", "/v1/vms/VM-00001", timeout=(5, 30), attempts=3, budget_seconds=30)

		# Every attempt is narrowed to what the shared deadline still allows, so
		# the repeated call never costs more than a single 30 second read.
		self.assertLessEqual(sum(read for _, read in seen), 30)
		self.assertLess(seen[-1][1], seen[0][1])

	def test_a_fast_failure_still_retries_inside_the_budget(self) -> None:
		client = build_client()
		responses = [requests.ConnectionError("refused"), build_response(200, {"state": "running"})]

		with patch("atlas.vm.core.metal_client.requests.Session.request", side_effect=responses) as request:
			body = client._request("GET", "/v1/vms/VM-00001", timeout=(5, 30), attempts=3, budget_seconds=30)

		self.assertEqual(body, {"state": "running"})
		self.assertEqual(request.call_count, 2)

	def test_migration_polling_is_not_repeated(self) -> None:
		"""The migration worker runs its own poll loop, so one read must not retry."""
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			side_effect=requests.ReadTimeout("read timed out"),
		) as request:
			with self.assertRaises(MetalClientError):
				client.get_migration("migration-1")

		self.assertEqual(request.call_count, 1)

	def test_a_status_read_gives_up_after_the_last_attempt(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			side_effect=requests.ReadTimeout("read timed out"),
		) as request:
			with self.assertRaises(MetalClientError):
				client.get_virtual_machine("VM-00001")

		self.assertEqual(request.call_count, client.status_attempts)

	def test_a_status_read_does_not_repeat_a_final_failure(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request", return_value=build_response(404)
		) as request:
			with self.assertRaises(MetalClientError):
				client.get_virtual_machine("VM-00001")

		self.assertEqual(request.call_count, 1)

	def test_a_write_is_never_repeated(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			side_effect=requests.ReadTimeout("read timed out"),
		) as request:
			with self.assertRaises(MetalClientError):
				client.put_virtual_machine("VM-00001", {})

		self.assertEqual(request.call_count, 1)


class TestMetalClientErrors(UnitTestCase):
	def test_transport_failure_is_retryable(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			side_effect=requests.ConnectionError("refused"),
		):
			with self.assertRaises(MetalClientError) as caught:
				client.get_virtual_machine("VM-00001")

		self.assertTrue(caught.exception.retryable)
		self.assertFalse(caught.exception.uncertain)

	def test_a_write_that_may_have_landed_is_uncertain(self) -> None:
		"""A lost response on a write must not be retried as if it never happened."""
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			side_effect=requests.ConnectionError("timeout"),
		):
			with self.assertRaises(MetalClientError) as caught:
				client.put_virtual_machine("VM-00001", {})

		self.assertTrue(caught.exception.uncertain)

	def test_metal_retryable_flag_wins_over_the_status_default(self) -> None:
		client = build_client()
		body = {"error": {"message": "shutting down", "code": "unavailable", "retryable": False}}

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request", return_value=build_response(503, body)
		):
			with self.assertRaises(MetalClientError) as caught:
				client.get_virtual_machine("VM-00001")

		self.assertFalse(caught.exception.retryable)
		self.assertEqual(caught.exception.code, "unavailable")

	def test_client_status_defaults_to_not_retryable(self) -> None:
		client = build_client()
		body = {"error": {"message": "bad request", "code": "invalid_request"}}

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request", return_value=build_response(400, body)
		):
			with self.assertRaises(MetalClientError) as caught:
				client.get_virtual_machine("VM-00001")

		self.assertFalse(caught.exception.retryable)

	def test_server_status_defaults_to_retryable(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			return_value=build_response(500, {"error": {"message": "boom"}}),
		):
			with self.assertRaises(MetalClientError) as caught:
				client.get_virtual_machine("VM-00001")

		self.assertTrue(caught.exception.retryable)

	def test_not_found_is_reported_for_deletion_decisions(self) -> None:
		"""Reconciliation deletes a record only on a confirmed absence."""
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			return_value=build_response(404, {"error": {"message": "gone"}}),
		):
			with self.assertRaises(MetalClientError) as caught:
				client.get_virtual_machine("VM-00001")

		self.assertTrue(caught.exception.is_not_found)

	def test_insufficient_capacity_is_told_apart_from_other_conflicts(self) -> None:
		client = build_client()

		for code, expected in (("insufficient_capacity", True), ("conflict", False)):
			with patch(
				"atlas.vm.core.metal_client.requests.Session.request",
				return_value=build_response(409, {"error": {"code": code, "message": "no room"}}),
			):
				with self.assertRaises(MetalClientError) as caught:
					client.set_virtual_machine_compute("VM-00001", {})

			self.assertEqual(caught.exception.is_insufficient_capacity, expected)

	def test_unparsable_error_body_keeps_the_status(self) -> None:
		client = build_client()
		response = build_response(502)
		response.json.side_effect = ValueError("not json")

		with patch("atlas.vm.core.metal_client.requests.Session.request", return_value=response):
			with self.assertRaises(MetalClientError) as caught:
				client.get_virtual_machine("VM-00001")

		self.assertEqual(caught.exception.status, 502)
		self.assertTrue(caught.exception.retryable)

	def test_non_object_response_is_rejected(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			return_value=build_response(200, ["not", "an", "object"], content=b"[]"),
		):
			with self.assertRaises(MetalClientError):
				client.get_virtual_machine("VM-00001")

	def test_empty_body_is_accepted_for_a_no_content_route(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			return_value=build_response(204, content=b""),
		):
			self.assertIsNone(client.delete_snapshot("SNAP-1"))


class TestMetalClientPaths(UnitTestCase):
	def test_identifiers_are_escaped_in_the_path(self) -> None:
		"""A name is caller-supplied, so it must never change the route shape."""
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			return_value=build_response(204, content=b""),
		) as request:
			client.delete_snapshot("a/b")

		self.assertEqual(request.call_args.args[1], "https://10.0.0.2:9000/v1/snapshots/a%2Fb")
		self.assertEqual(request.call_args.kwargs["verify"], client.ca_file)
		self.assertEqual(request.call_args.kwargs["cert"], client.client_certificate)

	def test_snapshot_routes_use_the_versioned_paths(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			return_value=build_response(202, content=b""),
		) as request:
			client.start_snapshot_upload("SNAP-1", {"rootfs": {"parts": []}})

		self.assertEqual(
			request.call_args.args[:2], ("POST", "https://10.0.0.2:9000/v1/snapshots/SNAP-1/upload")
		)


def virtual_machine_response() -> dict:
	"""Return the virtual machine body that every mutation route answers with."""
	return {
		"id": "VM-00001",
		"desired": {
			"generation": 1,
			"restart_generation": 0,
			"state": "running",
			"compute": {"cpu_millicores": 2000, "memory_mib": 2048, "sleep_after_idle_seconds": 0},
			"disk": {"size_mib": 2048, "throughput_mibps": 50, "iops": 2000},
			"image": {
				"ref": "ubuntu",
				"architecture": "amd64",
				"rootfs": {"sha256": "a" * 64},
				"kernel": {"sha256": "b" * 64},
				"cache_image": False,
				"memory_snapshot": False,
			},
			"network": {
				"egress": "uplink",
				"wireguard_mesh_ipv6": "",
				"private_network_throughput_mibps": 0,
				"public_network_throughput_mibps": 0,
				"firewall": {"enabled": False, "inbound": [], "outbound": []},
			},
			"guest": {"hostname": "", "ssh_keys": [], "metadata": {}},
		},
		"observed": {
			"generation": 0,
			"restart_generation": 0,
			"state": "unknown",
			"updated_at": "2026-09-06T10:00:00Z",
			"disk": {"used_mib": 0},
			"network": {},
		},
	}


COMPUTE_REQUEST = {
	"cpu_millicores": 2000,
	"memory_mib": 2048,
	"sleep_after_idle_seconds": 1800,
}


class TestMetalClientConnection(UnitTestCase):
	@patch("atlas.vm.core.metal_client.client_certificate_files", return_value=("atlas.crt", "atlas.key"))
	@patch("atlas.vm.core.metal_client.ca_file", return_value="ca.crt")
	def test_client_uses_the_validated_private_ipv4_address_by_default(
		self, _ca_file: Mock, _certificate_files: Mock
	) -> None:
		server = SimpleNamespace(
			name="Server-1",
			private_ipv4_address="10.0.0.2",
			settings=SimpleNamespace(use_public_ip_for_metald=False),
		)
		client = MetalClient(server)

		self.assertEqual(client.base_url, "https://10.0.0.2:9000")
		self.assertEqual(client.ca_file, "ca.crt")
		self.assertEqual(client.client_certificate, ("atlas.crt", "atlas.key"))

	@patch("atlas.vm.core.metal_client.client_certificate_files", return_value=("atlas.crt", "atlas.key"))
	@patch("atlas.vm.core.metal_client.ca_file", return_value="ca.crt")
	def test_client_uses_the_public_ipv4_address_when_configured(
		self, _ca_file: Mock, _certificate_files: Mock
	) -> None:
		server = SimpleNamespace(
			name="Server-1",
			public_ipv4_address="203.0.113.8",
			settings=SimpleNamespace(use_public_ip_for_metald=True),
		)
		client = MetalClient(server)

		self.assertEqual(client.base_url, "https://203.0.113.8:9000")

	@patch("atlas.vm.core.metal_client.client_certificate_files")
	@patch("atlas.vm.core.metal_client.ca_file")
	def test_client_rejects_an_invalid_private_ipv4_address(
		self, ca_file_mock: Mock, certificate_files: Mock
	) -> None:
		server = SimpleNamespace(
			name="Server-1",
			private_ipv4_address="not-an-address",
			settings=SimpleNamespace(use_public_ip_for_metald=False),
		)

		with self.assertRaisesRegex(MetalClientError, "invalid private IPv4 address"):
			MetalClient(server)

		ca_file_mock.assert_not_called()
		certificate_files.assert_not_called()

	def test_console_connection_builds_websocket_url(self) -> None:
		client = build_client()

		connection = client.get_console_connection("VM-00001")
		ssh_connection = client.get_console_connection("VM-00001", "ssh")

		self.assertEqual(connection["url"], "wss://10.0.0.2:9000/v1/vms/VM-00001/console?mode=tty")
		self.assertEqual(ssh_connection["url"], "wss://10.0.0.2:9000/v1/vms/VM-00001/console?mode=ssh")


class TestMetalClientVirtualMachineRoutes(UnitTestCase):
	def test_put_uses_the_atlas_vm_name(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			return_value=build_response(202, virtual_machine_response()),
		) as request:
			client.put_virtual_machine("VM-00001", {"cpu_millicores": 1000})

		self.assertEqual(request.call_args.args[:2], ("PUT", "https://10.0.0.2:9000/v1/vms/VM-00001"))

	def test_mutations_use_versioned_subresources(self) -> None:
		client = build_client()
		disk = {"size_mib": 2048, "throughput_mibps": 50, "iops": 2000}
		network = {
			"egress": "uplink",
			"public_ipv4": "203.0.113.10",
			"wireguard_mesh_ipv6": "fdaa:1::1",
			"private_network_throughput_mibps": 100,
			"public_network_throughput_mibps": 50,
		}

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			return_value=build_response(202, virtual_machine_response()),
		) as request:
			client.set_virtual_machine_power_state("VM-00001", "running")
			client.request_virtual_machine_restart("VM-00001")
			client.set_virtual_machine_disk("VM-00001", disk)
			client.set_virtual_machine_compute("VM-00001", COMPUTE_REQUEST)
			client.set_virtual_machine_network("VM-00001", network)
			client.replace_virtual_machine_ssh_keys("VM-00001", ["ssh-ed25519 AAAA"])
			client.replace_virtual_machine_metadata("VM-00001", {"env": "prod"})
			client.delete_virtual_machine("VM-00001")

		self.assertEqual(
			[call.args[:2] for call in request.call_args_list],
			[
				("PUT", "https://10.0.0.2:9000/v1/vms/VM-00001/power"),
				("POST", "https://10.0.0.2:9000/v1/vms/VM-00001/restart"),
				("PUT", "https://10.0.0.2:9000/v1/vms/VM-00001/disk"),
				("PUT", "https://10.0.0.2:9000/v1/vms/VM-00001/compute"),
				("PUT", "https://10.0.0.2:9000/v1/vms/VM-00001/network"),
				("PUT", "https://10.0.0.2:9000/v1/vms/VM-00001/ssh-keys"),
				("PUT", "https://10.0.0.2:9000/v1/vms/VM-00001/metadata"),
				("DELETE", "https://10.0.0.2:9000/v1/vms/VM-00001"),
			],
		)
		bodies = [call.kwargs.get("json") for call in request.call_args_list]
		self.assertEqual(bodies[2], disk)
		self.assertEqual(bodies[3], COMPUTE_REQUEST)
		self.assertEqual(bodies[4], network)
		self.assertEqual(bodies[5], {"ssh_keys": ["ssh-ed25519 AAAA"]})
		self.assertEqual(bodies[6], {"metadata": {"env": "prod"}})

	def test_snapshot_calls_use_unified_image_paths(self) -> None:
		client = build_client()
		responses = [
			build_response(201),
			build_response(202, content=b""),
			build_response(200, {"state": "uploading"}),
			build_response(204, content=b""),
		]

		with patch("atlas.vm.core.metal_client.requests.Session.request", side_effect=responses) as request:
			client.create_snapshot("VM-00001")
			client.start_snapshot_upload("image-1", {"rootfs": {"parts": []}, "kernel": {"parts": []}})
			client.get_snapshot("image-1")
			client.delete_snapshot("image-1")

		self.assertEqual(
			[call.args[:2] for call in request.call_args_list],
			[
				("POST", "https://10.0.0.2:9000/v1/vms/VM-00001/snapshots"),
				("POST", "https://10.0.0.2:9000/v1/snapshots/image-1/upload"),
				("GET", "https://10.0.0.2:9000/v1/snapshots/image-1"),
				("DELETE", "https://10.0.0.2:9000/v1/snapshots/image-1"),
			],
		)
		self.assertEqual(request.call_args_list[0].kwargs["timeout"], client.snapshot_timeout_seconds)
		self.assertEqual(request.call_args_list[1].kwargs["timeout"], client.snapshot_timeout_seconds)

	def test_sync_sends_wireguard_peers_images_and_privileged_addresses(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			return_value=build_response(200, {"capacity": {}}),
		) as request:
			result = client.sync([{"node": "node-1"}], [{"ref": "sha256:image"}], ["fdaa:1::1"])

		self.assertEqual(result, {"capacity": {}})
		self.assertEqual(request.call_args.args[:2], ("POST", "https://10.0.0.2:9000/v1/sync"))
		self.assertEqual(
			request.call_args.kwargs["json"],
			{
				"wireguard_peers": [{"node": "node-1"}],
				"images": [{"ref": "sha256:image"}],
				"privileged_vm_addresses": ["fdaa:1::1"],
			},
		)

	def test_transport_error_marks_every_write_as_uncertain(self) -> None:
		client = build_client()
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
			"snapshot_upload": lambda: client.start_snapshot_upload("image-1", {}),
			"sync": lambda: client.sync([], [], []),
		}

		for operation_name, write_operation in write_operations.items():
			with (
				self.subTest(operation=operation_name),
				patch(
					"atlas.vm.core.metal_client.requests.Session.request",
					side_effect=requests.ConnectionError("lost"),
				),
				self.assertRaises(MetalClientError) as raised,
			):
				write_operation()

			self.assertTrue(raised.exception.uncertain)


class TestMetalClientMigrations(UnitTestCase):
	def test_put_migration_sends_the_target_pull_request(self) -> None:
		client = build_client()
		migration = {"status": "running"}

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			return_value=build_response(202, migration),
		) as request:
			result = client.put_migration("mig-00001", "vm-00001", "http://10.0.0.3:9000")

		self.assertEqual(result, migration)
		self.assertEqual(request.call_args.args[:2], ("PUT", "https://10.0.0.2:9000/v1/migrations/mig-00001"))
		# The keys must match Metal's createMigrationRequest, which decodes strictly.
		self.assertEqual(
			request.call_args.kwargs["json"],
			{"virtual_machine_id": "vm-00001", "source": "http://10.0.0.3:9000"},
		)

	def test_put_migration_sends_an_optional_resize(self) -> None:
		client = build_client()
		resize = {"cpu_millicores": 4000, "memory_mib": 8192, "disk_mib": 40960}

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			return_value=build_response(202, {"status": "running"}),
		) as request:
			client.put_migration("mig-00001", "vm-00001", "http://10.0.0.3:9000", resize)

		self.assertEqual(request.call_args.kwargs["json"]["resize"], resize)

	def test_get_migration_reads_status(self) -> None:
		client = build_client()
		progress = {"status": "running", "phase": "copying"}

		with patch(
			"atlas.vm.core.metal_client.requests.Session.request",
			return_value=build_response(200, progress),
		) as request:
			result = client.get_migration("mig-00001")

		self.assertEqual(result, progress)
		self.assertEqual(request.call_args.args[:2], ("GET", "https://10.0.0.2:9000/v1/migrations/mig-00001"))

	def test_abort_and_finish_use_their_routes(self) -> None:
		client = build_client()

		for method_name, suffix in (("abort_migration", "abort"), ("finish_migration", "finish")):
			with patch(
				"atlas.vm.core.metal_client.requests.Session.request",
				return_value=build_response(202, content=b""),
			) as request:
				getattr(client, method_name)("mig-00001")

			self.assertEqual(
				request.call_args.args[:2],
				("POST", f"https://10.0.0.2:9000/v1/migrations/mig-00001/{suffix}"),
			)

	def test_repeatable_calls_report_an_uncertain_transport_failure(self) -> None:
		"""A lost response on a repeatable call must not read as a definite failure."""
		client = build_client()

		for call_migration in (
			lambda: client.put_migration("mig-00001", "vm-00001", "http://10.0.0.3:9000"),
			lambda: client.abort_migration("mig-00001"),
			lambda: client.finish_migration("mig-00001"),
		):
			with patch(
				"atlas.vm.core.metal_client.requests.Session.request",
				side_effect=requests.ConnectionError("timeout"),
			):
				with self.assertRaises(MetalClientError) as caught:
					call_migration()

			self.assertTrue(caught.exception.uncertain)

	def test_api_url_rejects_a_server_without_an_address(self) -> None:
		server = Mock(private_ipv4_address="", settings=SimpleNamespace(use_public_ip_for_metald=False))
		server.name = "metal-1"

		with self.assertRaises(MetalClientError):
			MetalClient.get_api_url(server)

	def test_coordination_url_uses_the_wireguard_address(self) -> None:
		server = Mock(wireguard_ip_address="fdab::12")
		server.name = "metal-1"

		self.assertEqual(MetalClient.get_coordination_url(server), "https://[fdab::12]:9001")
