from __future__ import annotations

from typing import Any
from unittest.mock import Mock, patch

import requests
from frappe.tests import UnitTestCase

from atlas.vm.core.metal_client import MetalClient, MetalClientError


def build_client() -> MetalClient:
	client = MetalClient.__new__(MetalClient)
	client.base_url = "http://10.0.0.2:9000"
	client.headers = {"Authorization": "Bearer token"}
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

		with patch("atlas.vm.core.metal_client.requests.request", side_effect=responses) as request:
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
			patch("atlas.vm.core.metal_client.requests.request", side_effect=record),
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

		with patch("atlas.vm.core.metal_client.requests.request", side_effect=responses) as request:
			body = client._request("GET", "/v1/vms/VM-00001", timeout=(5, 30), attempts=3, budget_seconds=30)

		self.assertEqual(body, {"state": "running"})
		self.assertEqual(request.call_count, 2)

	def test_migration_polling_is_not_repeated(self) -> None:
		"""The migration worker runs its own poll loop, so one read must not retry."""
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			side_effect=requests.ReadTimeout("read timed out"),
		) as request:
			with self.assertRaises(MetalClientError):
				client.get_migration("migration-1")

		self.assertEqual(request.call_count, 1)

	def test_a_status_read_gives_up_after_the_last_attempt(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			side_effect=requests.ReadTimeout("read timed out"),
		) as request:
			with self.assertRaises(MetalClientError):
				client.get_virtual_machine("VM-00001")

		self.assertEqual(request.call_count, client.status_attempts)

	def test_a_status_read_does_not_repeat_a_final_failure(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request", return_value=build_response(404)
		) as request:
			with self.assertRaises(MetalClientError):
				client.get_virtual_machine("VM-00001")

		self.assertEqual(request.call_count, 1)

	def test_a_write_is_never_repeated(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			side_effect=requests.ReadTimeout("read timed out"),
		) as request:
			with self.assertRaises(MetalClientError):
				client.put_virtual_machine("VM-00001", {})

		self.assertEqual(request.call_count, 1)


class TestMetalClientErrors(UnitTestCase):
	def test_transport_failure_is_retryable(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request",
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
			"atlas.vm.core.metal_client.requests.request",
			side_effect=requests.ConnectionError("timeout"),
		):
			with self.assertRaises(MetalClientError) as caught:
				client.put_virtual_machine("VM-00001", {})

		self.assertTrue(caught.exception.uncertain)

	def test_metal_retryable_flag_wins_over_the_status_default(self) -> None:
		client = build_client()
		body = {"error": {"message": "shutting down", "code": "unavailable", "retryable": False}}

		with patch("atlas.vm.core.metal_client.requests.request", return_value=build_response(503, body)):
			with self.assertRaises(MetalClientError) as caught:
				client.get_virtual_machine("VM-00001")

		self.assertFalse(caught.exception.retryable)
		self.assertEqual(caught.exception.code, "unavailable")

	def test_client_status_defaults_to_not_retryable(self) -> None:
		client = build_client()
		body = {"error": {"message": "bad request", "code": "invalid_request"}}

		with patch("atlas.vm.core.metal_client.requests.request", return_value=build_response(400, body)):
			with self.assertRaises(MetalClientError) as caught:
				client.get_virtual_machine("VM-00001")

		self.assertFalse(caught.exception.retryable)

	def test_server_status_defaults_to_retryable(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			return_value=build_response(500, {"error": {"message": "boom"}}),
		):
			with self.assertRaises(MetalClientError) as caught:
				client.get_virtual_machine("VM-00001")

		self.assertTrue(caught.exception.retryable)

	def test_not_found_is_reported_for_deletion_decisions(self) -> None:
		"""Reconciliation deletes a record only on a confirmed absence."""
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			return_value=build_response(404, {"error": {"message": "gone"}}),
		):
			with self.assertRaises(MetalClientError) as caught:
				client.get_virtual_machine("VM-00001")

		self.assertTrue(caught.exception.is_not_found)

	def test_unparsable_error_body_keeps_the_status(self) -> None:
		client = build_client()
		response = build_response(502)
		response.json.side_effect = ValueError("not json")

		with patch("atlas.vm.core.metal_client.requests.request", return_value=response):
			with self.assertRaises(MetalClientError) as caught:
				client.get_virtual_machine("VM-00001")

		self.assertEqual(caught.exception.status, 502)
		self.assertTrue(caught.exception.retryable)

	def test_non_object_response_is_rejected(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			return_value=build_response(200, ["not", "an", "object"], content=b"[]"),
		):
			with self.assertRaises(MetalClientError):
				client.get_virtual_machine("VM-00001")

	def test_empty_body_is_accepted_for_a_no_content_route(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			return_value=build_response(204, content=b""),
		):
			self.assertIsNone(client.delete_snapshot("SNAP-1"))


class TestMetalClientPaths(UnitTestCase):
	def test_identifiers_are_escaped_in_the_path(self) -> None:
		"""A name is caller-supplied, so it must never change the route shape."""
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			return_value=build_response(204, content=b""),
		) as request:
			client.delete_snapshot("a/b")

		self.assertEqual(request.call_args.args[1], "http://10.0.0.2:9000/v1/snapshots/a%2Fb")

	def test_snapshot_routes_use_the_versioned_paths(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			return_value=build_response(202, content=b""),
		) as request:
			client.start_snapshot_upload("SNAP-1", {"rootfs": {"parts": []}})

		self.assertEqual(
			request.call_args.args[:2], ("POST", "http://10.0.0.2:9000/v1/snapshots/SNAP-1/upload")
		)

	def test_sync_omits_the_unicast_field_in_multicast_mode(self) -> None:
		"""Metal decodes the sync request strictly, so an unknown field fails the exchange."""
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			return_value=build_response(200, {"capacity": {}}),
		) as request:
			client.sync([], [], [])

		self.assertEqual(request.call_args.args[:2], ("POST", "http://10.0.0.2:9000/v1/sync"))
		self.assertEqual(
			request.call_args.kwargs["json"],
			{"wireguard_peers": [], "images": [], "privileged_vm_addresses": []},
		)

	def test_sync_carries_the_unicast_peers_in_unicast_mode(self) -> None:
		client = build_client()

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			return_value=build_response(200, {"capacity": {}}),
		) as request:
			client.sync([], [], [], unicast_peers=["10.20.0.11"])

		self.assertEqual(
			request.call_args.kwargs["json"],
			{
				"wireguard_peers": [],
				"images": [],
				"privileged_vm_addresses": [],
				"unicast_peers": ["10.20.0.11"],
			},
		)


class TestMetalClientMigrations(UnitTestCase):
	def test_put_migration_sends_the_target_pull_request(self) -> None:
		client = build_client()
		migration = {"status": "running"}

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			return_value=build_response(202, migration),
		) as request:
			result = client.put_migration("mig-00001", "vm-00001", "http://10.0.0.3:9000")

		self.assertEqual(result, migration)
		self.assertEqual(request.call_args.args[:2], ("PUT", "http://10.0.0.2:9000/v1/migrations/mig-00001"))
		# The keys must match Metal's createMigrationRequest, which decodes strictly.
		self.assertEqual(
			request.call_args.kwargs["json"],
			{"virtual_machine_id": "vm-00001", "source": "http://10.0.0.3:9000"},
		)

	def test_get_migration_reads_status(self) -> None:
		client = build_client()
		progress = {"status": "running", "phase": "copying"}

		with patch(
			"atlas.vm.core.metal_client.requests.request",
			return_value=build_response(200, progress),
		) as request:
			result = client.get_migration("mig-00001")

		self.assertEqual(result, progress)
		self.assertEqual(request.call_args.args[:2], ("GET", "http://10.0.0.2:9000/v1/migrations/mig-00001"))

	def test_abort_and_finish_use_their_routes(self) -> None:
		client = build_client()

		for method_name, suffix in (("abort_migration", "abort"), ("finish_migration", "finish")):
			with patch(
				"atlas.vm.core.metal_client.requests.request",
				return_value=build_response(202, content=b""),
			) as request:
				getattr(client, method_name)("mig-00001")

			self.assertEqual(
				request.call_args.args[:2],
				("POST", f"http://10.0.0.2:9000/v1/migrations/mig-00001/{suffix}"),
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
				"atlas.vm.core.metal_client.requests.request",
				side_effect=requests.ConnectionError("timeout"),
			):
				with self.assertRaises(MetalClientError) as caught:
					call_migration()

			self.assertTrue(caught.exception.uncertain)

	def test_api_url_rejects_a_server_without_an_address(self) -> None:
		server = Mock(public_ipv4_address="")
		server.name = "metal-1"

		with self.assertRaises(MetalClientError):
			MetalClient.get_api_url(server)
