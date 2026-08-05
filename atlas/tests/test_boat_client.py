"""Unit tests for the Boat client — the Atlas half of the Atlas↔Boat seam (spec/33, WO-0).

No daemon runs here: `requests.request` is patched throughout, which is also the
point. These prove the exact wire shape Atlas sends, that a failure at that
boundary raises instead of quietly finding another way onto the host, that a Fake
host is never called at all, and that the VM
lifecycle takes the same SSH path it always took.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from atlas.atlas.boat_client import (
	BoatClient,
	BoatError,
	base_url_for_server,
	run_boat_task,
	token_for_server,
)
from atlas.atlas.doctype.virtual_machine import virtual_machine as virtual_machine_module
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.tests import fixtures
from atlas.tests._mocks import fake_task

REQUEST = "atlas.atlas.boat_client.requests.request"


class _Response:
	"""The slice of `requests.Response` the client reads."""

	def __init__(self, status_code: int = 200, payload: dict | list | None = None, text: str = ""):
		self.status_code = status_code
		self._payload = payload
		self.text = text or (json.dumps(payload) if payload is not None else "")
		self.content = self.text.encode()

	def json(self):
		if self._payload is None:
			raise ValueError("response body is not JSON")
		return self._payload


def _operation(status: str = "Success", **overrides) -> dict:
	"""An Operation record shaped like `api/openapi.yaml`'s schema."""
	record = {
		"operation_id": "task-boat-1",
		"verb": "start-vm",
		"uuid": "vm-1",
		"status": status,
		"started_at": "2026-07-27T10:00:00Z",
		"ended_at": "2026-07-27T10:00:02Z",
		"exit_code": 0 if status == "Success" else 1,
		"output": "started\n",
		"error": "",
	}
	record.update(overrides)
	return record


def _patch_conf(overrides: dict):
	"""Patch `frappe.conf` (== `frappe.local.conf`, a _dict) with `overrides`."""
	return patch.dict(frappe.local.conf, overrides)


def _boat_host_token(server_name: str):
	return _patch_conf({"atlas_boat_tokens": {server_name: "s3cret"}, "atlas_boat_token": None})


def _boat_server() -> "frappe.model.document.Document":
	"""A real-provider (non-Fake) Active host with an address Boat can be reached at."""
	provider = fixtures.make_provider("boat-test-provider")
	return fixtures.make_server(
		provider,
		"boat-test-server",
		ipv4_address="198.51.100.7",
		ipv6_address="2001:db8:b0a7::1",
		ipv6_prefix="2001:db8:b0a7::/64",
		ipv6_virtual_machine_range="2001:db8:b0a7::/124",
		status="Active",
	)


def _clear_virtual_machines() -> None:
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestBoatClientWire(IntegrationTestCase):
	"""What Atlas actually puts on the wire for each WO-0 endpoint."""

	def setUp(self) -> None:
		self.client = BoatClient(base_url="http://198.51.100.7:8080/v1", token="s3cret")

	def test_start_posts_the_operation_identifier(self) -> None:
		with patch(REQUEST, return_value=_Response(payload=_operation())) as request:
			operation = self.client.start_virtual_machine("vm-1", operation_id="task-boat-1")

		self.assertEqual(operation["status"], "Success")
		method, url = request.call_args.args
		self.assertEqual(method, "POST")
		self.assertEqual(url, "http://198.51.100.7:8080/v1/vms/vm-1/start")
		self.assertEqual(request.call_args.kwargs["json"], {"operation_id": "task-boat-1"})

	def test_start_sends_the_bearer_token(self) -> None:
		with patch(REQUEST, return_value=_Response(payload=_operation())) as request:
			self.client.start_virtual_machine("vm-1", operation_id="task-boat-1")

		headers = request.call_args.kwargs["headers"]
		self.assertEqual(headers["Authorization"], "Bearer s3cret")
		self.assertEqual(headers["Accept"], "application/json")

	def test_stop_sends_graceful_and_the_bounded_drain(self) -> None:
		with patch(REQUEST, return_value=_Response(payload=_operation(verb="stop-vm"))) as request:
			self.client.stop_virtual_machine(
				"vm-1", operation_id="task-boat-2", graceful=False, stop_timeout_seconds=20
			)

		self.assertEqual(request.call_args.args[1], "http://198.51.100.7:8080/v1/vms/vm-1/stop")
		self.assertEqual(
			request.call_args.kwargs["json"],
			{"operation_id": "task-boat-2", "graceful": False, "stop_timeout_seconds": 20},
		)

	def test_stop_omits_a_zero_timeout_so_systemd_keeps_its_default(self) -> None:
		with patch(REQUEST, return_value=_Response(payload=_operation(verb="stop-vm"))) as request:
			self.client.stop_virtual_machine("vm-1", operation_id="task-boat-2")

		self.assertEqual(request.call_args.kwargs["json"], {"operation_id": "task-boat-2", "graceful": True})

	def test_read_endpoints_hit_their_paths(self) -> None:
		cases = (
			(lambda: self.client.get_virtual_machine("vm-1"), "GET", "/vms/vm-1"),
			(lambda: self.client.list_virtual_machines(), "GET", "/vms"),
			(lambda: self.client.get_host(), "GET", "/host"),
			(lambda: self.client.get_operation("task-boat-1"), "GET", "/ops/task-boat-1"),
		)
		for call, method, path in cases:
			with self.subTest(path=path), patch(REQUEST, return_value=_Response(payload={})) as request:
				call()
				self.assertEqual(request.call_args.args[0], method)
				self.assertEqual(request.call_args.args[1], f"http://198.51.100.7:8080/v1{path}")

	def test_non_2xx_raises_with_the_daemons_own_sentence(self) -> None:
		response = _Response(status_code=404, payload={"error": "no such VM on this host"})
		with patch(REQUEST, return_value=response), self.assertRaises(BoatError) as raised:
			self.client.start_virtual_machine("vm-gone", operation_id="task-boat-1")

		self.assertIn("no such VM on this host", str(raised.exception))
		self.assertIn("404", str(raised.exception))
		# The token is a credential, not diagnostic detail.
		self.assertNotIn("s3cret", str(raised.exception))

	def test_untyped_error_body_falls_back_to_the_raw_text(self) -> None:
		response = _Response(status_code=502, text="<html>bad gateway</html>")
		with patch(REQUEST, return_value=response), self.assertRaises(BoatError) as raised:
			self.client.get_host()

		self.assertIn("bad gateway", str(raised.exception))

	def test_transport_failure_raises_rather_than_degrading(self) -> None:
		with (
			patch(REQUEST, side_effect=requests.ConnectionError("connection refused")),
			self.assertRaises(BoatError) as raised,
		):
			self.client.start_virtual_machine("vm-1", operation_id="task-boat-1")

		self.assertIn("connection refused", str(raised.exception))


class TestBoatCredentials(IntegrationTestCase):
	def setUp(self) -> None:
		self.server = _boat_server()

	def test_base_url_defaults_to_the_management_tunnel_and_never_the_public_address(self) -> None:
		"""The mesh address, and specifically NOT `ipv4_address`.

		Boat binds the management tunnel and the local socket, so the mesh
		endpoint is the only address it answers on — and it is the only one that
		keeps the bearer token inside the tunnel. `ipv4_address` is the host's
		PUBLIC address, the one SSH uses; defaulting to it would put a long-lived
		token on the public internet in cleartext on every verb and every mirror
		poll."""
		mesh = frappe.db.get_value("Server", self.server.name, "mesh_address")
		with _patch_conf({"atlas_boat_base_urls": None, "atlas_boat_port": None}):
			url = base_url_for_server(self.server.name)

		self.assertEqual(url, f"http://[{mesh}]:8080/v1")
		self.assertNotIn("198.51.100.7", url)

	def test_site_config_can_point_a_host_elsewhere(self) -> None:
		with _patch_conf({"atlas_boat_base_urls": {self.server.name: "http://127.0.0.1:9000/v1"}}):
			self.assertEqual(base_url_for_server(self.server.name), "http://127.0.0.1:9000/v1")

	def test_base_url_throws_without_a_mesh_address(self) -> None:
		"""Refuse rather than fall back. A host with no mesh address has no
		tunnel to reach Boat over, and the only other address available is the
		public one — which is the thing this must never quietly use."""
		frappe.db.set_value("Server", self.server.name, "mesh_address", None)
		with _patch_conf({"atlas_boat_base_urls": None}), self.assertRaises(frappe.ValidationError):
			base_url_for_server(self.server.name)

	def test_per_host_token_wins_over_the_single_host_fallback(self) -> None:
		with _patch_conf({"atlas_boat_tokens": {self.server.name: "per-host"}, "atlas_boat_token": "fleet"}):
			self.assertEqual(token_for_server(self.server.name), "per-host")

	def test_single_host_fallback_token(self) -> None:
		with _patch_conf({"atlas_boat_tokens": None, "atlas_boat_token": "fleet"}):
			self.assertEqual(token_for_server(self.server.name), "fleet")

	def test_missing_token_throws(self) -> None:
		with (
			_patch_conf({"atlas_boat_tokens": None, "atlas_boat_token": None}),
			self.assertRaises(frappe.ValidationError),
		):
			token_for_server(self.server.name)


class TestRunBoatTask(IntegrationTestCase):
	"""The Task row a Boat verb leaves behind — the operator-facing surface that
	must be indistinguishable from an SSH run's."""

	def setUp(self) -> None:
		self.server = _boat_server()
		self.image = fixtures.make_image("boat-test-image")
		_clear_virtual_machines()
		self.virtual_machine = fixtures.make_virtual_machine(self.server.name, self.image.name)

	def _run(self, script: str, variables: dict, response: _Response):
		with _boat_host_token(self.server.name), patch(REQUEST, return_value=response) as request:
			task = run_boat_task(
				server=self.server.name,
				script=script,
				variables=variables,
				virtual_machine=self.virtual_machine.name,
				timeout_seconds=30,
			)
		return task, request

	def _last_task(self) -> "frappe.model.document.Document":
		return frappe.get_last_doc("Task", filters={"virtual_machine": self.virtual_machine.name})

	def test_success_folds_the_daemons_output_onto_the_task_row(self) -> None:
		operation = _operation(output="+ systemctl start firecracker-vm@x\nstarted\n")
		task, _request = self._run(
			"start-vm", {"VIRTUAL_MACHINE_NAME": self.virtual_machine.name}, _Response(payload=operation)
		)

		self.assertEqual(task.status, "Success")
		self.assertEqual(task.stdout, "+ systemctl start firecracker-vm@x\nstarted\n")
		self.assertEqual(task.stderr, "")
		self.assertEqual(task.exit_code, 0)
		self.assertEqual(task.script, "start-vm")
		self.assertEqual(task.server, self.server.name)
		self.assertEqual(task.virtual_machine, self.virtual_machine.name)
		self.assertIsNotNone(task.started)
		self.assertIsNotNone(task.ended)

	def test_the_operation_identifier_is_the_task_name(self) -> None:
		# This identity is the whole replay contract: re-posting it returns the
		# recorded result instead of booting the VM a second time.
		task, request = self._run(
			"start-vm", {"VIRTUAL_MACHINE_NAME": self.virtual_machine.name}, _Response(payload=_operation())
		)
		self.assertEqual(request.call_args.kwargs["json"]["operation_id"], task.name)

	def test_the_verb_addresses_the_vm_by_uuid_within_the_task_timeout(self) -> None:
		_task, request = self._run(
			"start-vm", {"VIRTUAL_MACHINE_NAME": self.virtual_machine.name}, _Response(payload=_operation())
		)
		self.assertTrue(request.call_args.args[1].endswith(f"/vms/{self.virtual_machine.name}/start"))
		self.assertEqual(request.call_args.kwargs["timeout"], 30)

	def test_stop_variables_become_the_stop_request_body(self) -> None:
		_task, request = self._run(
			"stop-vm",
			{
				"VIRTUAL_MACHINE_NAME": self.virtual_machine.name,
				"GRACEFUL": "0",
				"STOP_TIMEOUT_SECONDS": "20",
			},
			_Response(payload=_operation(verb="stop-vm")),
		)
		self.assertEqual(request.call_args.kwargs["json"]["graceful"], False)
		self.assertEqual(request.call_args.kwargs["json"]["stop_timeout_seconds"], 20)

	def test_reserved_ip_variables_become_the_reserved_ip_body(self) -> None:
		_task, request = self._run(
			"vm-reserved-ip",
			{
				"VIRTUAL_MACHINE_NAME": self.virtual_machine.name,
				"RESERVED_IPV4": "146.190.11.153",
				"ACTION": "attach",
			},
			_Response(payload=_operation(verb="vm-reserved-ip")),
		)
		self.assertTrue(request.call_args.args[1].endswith(f"/vms/{self.virtual_machine.name}/reserved-ip"))
		self.assertEqual(request.call_args.kwargs["json"]["action"], "attach")
		self.assertEqual(request.call_args.kwargs["json"]["reserved_ipv4"], "146.190.11.153")

	def test_reserved_ip_omits_the_address_when_it_is_not_given(self) -> None:
		# A detach keys on the guest, so the body carries no reserved IP — the daemon
		# tells it from an attach that forgot its address by its absence.
		_task, request = self._run(
			"vm-reserved-ip",
			{"VIRTUAL_MACHINE_NAME": self.virtual_machine.name, "ACTION": "detach"},
			_Response(payload=_operation(verb="vm-reserved-ip")),
		)
		self.assertEqual(request.call_args.kwargs["json"]["action"], "detach")
		self.assertNotIn("reserved_ipv4", request.call_args.kwargs["json"])

	def test_a_failed_operation_fails_the_task_and_raises(self) -> None:
		operation = _operation("Failure", output="trace\n", error="firecracker refused to start")
		with (
			_boat_host_token(self.server.name),
			patch(REQUEST, return_value=_Response(payload=operation)),
			self.assertRaises(frappe.ValidationError) as raised,
		):
			run_boat_task(
				server=self.server.name,
				script="start-vm",
				variables={"VIRTUAL_MACHINE_NAME": self.virtual_machine.name},
				virtual_machine=self.virtual_machine.name,
				timeout_seconds=30,
			)

		self.assertIn("firecracker refused to start", str(raised.exception))
		task = self._last_task()
		self.assertEqual(task.status, "Failure")
		self.assertEqual(task.stdout, "trace\n")
		self.assertEqual(task.stderr, "firecracker refused to start")
		self.assertEqual(task.exit_code, 1)

	def test_non_2xx_raises_and_never_falls_back_to_ssh(self) -> None:
		response = _Response(status_code=500, payload={"error": "thin pool is full"})
		with (
			_boat_host_token(self.server.name),
			patch(REQUEST, return_value=response),
			patch("atlas.atlas._ssh.runner.run_ssh") as run_ssh,
			self.assertRaises(frappe.ValidationError) as raised,
		):
			run_boat_task(
				server=self.server.name,
				script="start-vm",
				variables={"VIRTUAL_MACHINE_NAME": self.virtual_machine.name},
				virtual_machine=self.virtual_machine.name,
				timeout_seconds=30,
			)

		self.assertIn("thin pool is full", str(raised.exception))
		run_ssh.assert_not_called()
		self.assertEqual(self._last_task().status, "Failure")

	def test_a_non_terminal_operation_is_a_protocol_error(self) -> None:
		with (
			_boat_host_token(self.server.name),
			patch(REQUEST, return_value=_Response(payload=_operation("Running"))),
			self.assertRaises(frappe.ValidationError) as raised,
		):
			run_boat_task(
				server=self.server.name,
				script="start-vm",
				variables={"VIRTUAL_MACHINE_NAME": self.virtual_machine.name},
				virtual_machine=self.virtual_machine.name,
				timeout_seconds=30,
			)

		self.assertIn("not a terminal result", str(raised.exception))
		self.assertEqual(self._last_task().status, "Failure")

	def test_a_verb_boat_does_not_serve_raises_before_any_request(self) -> None:
		with (
			_boat_host_token(self.server.name),
			patch(REQUEST) as request,
			self.assertRaises(frappe.ValidationError) as raised,
		):
			run_boat_task(
				server=self.server.name,
				script="snapshot-stop-vm",
				variables={"VIRTUAL_MACHINE_NAME": self.virtual_machine.name},
				virtual_machine=self.virtual_machine.name,
				timeout_seconds=120,
			)

		self.assertIn("snapshot-stop-vm", str(raised.exception))
		request.assert_not_called()


class TestFakeServerIsNeverCalled(IntegrationTestCase):
	"""A Fake-backed host never gets a Boat call, exactly as it never gets an SSH
	connection — the dev/test fleet is Fake hosts, so this is what lets them run
	the same code paths a real host does."""

	def setUp(self) -> None:
		provider = fixtures.make_provider_row("boat-fake-provider", provider_type="Fake")
		fixtures.set_atlas_settings(provider)
		self.server = fixtures.make_server(
			provider,
			"boat-fake-server",
			ipv4_address="203.0.113.44",
			ipv6_address="2001:db8:fa4e::1",
			ipv6_prefix="2001:db8:fa4e::/64",
			ipv6_virtual_machine_range="2001:db8:fa4e::/124",
			status="Active",
		)
		self.image = fixtures.make_image("boat-fake-image")
		_clear_virtual_machines()
		self.virtual_machine = fixtures.make_virtual_machine(self.server.name, self.image.name)

	def test_fake_host_gets_a_synthesized_task_and_no_request(self) -> None:
		with patch(REQUEST) as request:
			task = run_boat_task(
				server=self.server.name,
				script="start-vm",
				variables={"VIRTUAL_MACHINE_NAME": self.virtual_machine.name},
				virtual_machine=self.virtual_machine.name,
				timeout_seconds=30,
			)

		request.assert_not_called()
		self.assertEqual(task.status, "Success")
		self.assertEqual(task.script, "start-vm")
		self.assertEqual(task.server, self.server.name)

	def test_fake_host_needs_no_boat_credentials(self) -> None:
		# No token, no base URL, nothing configured: the fake seam is taken first.
		with (
			_patch_conf({"atlas_boat_tokens": None, "atlas_boat_token": None}),
			patch(REQUEST) as request,
		):
			run_boat_task(
				server=self.server.name,
				script="start-vm",
				variables={"VIRTUAL_MACHINE_NAME": self.virtual_machine.name},
				virtual_machine=self.virtual_machine.name,
				timeout_seconds=30,
			)
		request.assert_not_called()


class TestVirtualMachineBoatBranch(IntegrationTestCase):
	"""`start()`/`stop()` go to the daemon and change nothing else.

	The two tests that proved the SSH path went with `boat_enabled`: there is no
	second transport left to pick, so "leaves start on the SSH path" is not a
	behaviour that can be asserted."""

	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_clear_virtual_machines()

	def _virtual_machine(self, status: str) -> "frappe.model.document.Document":
		virtual_machine = _new_vm()
		virtual_machine.status = status
		if status == "Running":
			virtual_machine.last_started = frappe.utils.now_datetime()
		elif status == "Stopped":
			virtual_machine.last_stopped = frappe.utils.now_datetime()
		virtual_machine.save(ignore_permissions=True)
		return virtual_machine

	def test_start_goes_through_boat_with_the_same_arguments(self) -> None:
		virtual_machine = self._virtual_machine("Stopped")
		# From WO-2 the verb is preceded by the desired state it acts on; the
		# argument list it runs with is unchanged (spec/33 §11.3).
		with (
			_boat_host_token(virtual_machine.server),
			patch(REQUEST, return_value=_Response(payload={})) as request,
			patch.object(virtual_machine_module, "run_task") as run_task,
			patch.object(
				virtual_machine_module, "run_boat_task", return_value=fake_task("task-boat-1")
			) as run_boat,
		):
			result = virtual_machine.start()

		self.assertEqual(result, "task-boat-1")
		self.assertEqual(request.call_args.args[0], "PUT")
		self.assertEqual(request.call_args.kwargs["json"]["desired_power"], "Running")
		run_task.assert_not_called()
		self.assertEqual(
			run_boat.call_args.kwargs,
			{
				"server": virtual_machine.server,
				"script": "start-vm",
				"variables": {"VIRTUAL_MACHINE_NAME": virtual_machine.name},
				"virtual_machine": virtual_machine.name,
				"timeout_seconds": 30,
			},
		)
		virtual_machine.reload()
		self.assertEqual(virtual_machine.status, "Running")

	def test_stop_goes_through_boat_with_the_same_arguments(self) -> None:
		virtual_machine = self._virtual_machine("Running")
		with (
			_boat_host_token(virtual_machine.server),
			patch(REQUEST, return_value=_Response(payload={})) as request,
			patch.object(virtual_machine_module, "run_task") as run_task,
			patch.object(
				virtual_machine_module, "run_boat_task", return_value=fake_task("task-boat-2")
			) as run_boat,
		):
			result = virtual_machine.stop(graceful=False, stop_timeout_seconds=20)

		self.assertEqual(result, "task-boat-2")
		self.assertEqual(request.call_args.kwargs["json"]["desired_power"], "Stopped")
		run_task.assert_not_called()
		self.assertEqual(
			run_boat.call_args.kwargs["variables"],
			{
				"VIRTUAL_MACHINE_NAME": virtual_machine.name,
				"GRACEFUL": "0",
				"STOP_TIMEOUT_SECONDS": "20",
			},
		)
		virtual_machine.reload()
		self.assertEqual(virtual_machine.status, "Stopped")

	def test_snapshot_stop_stays_on_ssh(self) -> None:
		# Boat still serves no snapshot-stop verb, so the memory-snapshot stop keeps
		# its SSH path. Its intent is stated all the same, or the host's reconciler
		# would boot the VM again the moment the unit went down (spec/33 §11.3).
		virtual_machine = self._virtual_machine("Running")
		snapshot_task = fake_task("task-snap-1", stdout='ATLAS_RESULT={"memory_snapshot": true}\n')
		with (
			_boat_host_token(virtual_machine.server),
			patch(REQUEST, return_value=_Response(payload={})) as request,
			patch.object(virtual_machine_module, "run_task", return_value=snapshot_task) as run_task,
			patch.object(virtual_machine_module, "run_boat_task") as run_boat,
		):
			virtual_machine.stop(memory_snapshot=True)

		run_boat.assert_not_called()
		self.assertEqual(run_task.call_args.kwargs["script"], "snapshot-stop-vm")
		self.assertEqual(request.call_args.args[0], "PUT")
		self.assertEqual(request.call_args.kwargs["json"]["desired_power"], "Stopped")
