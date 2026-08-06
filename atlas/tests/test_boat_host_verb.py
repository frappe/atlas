"""The host-verb transport — the Atlas half of `POST /host-verbs/{verb}`.

The host verbs (provision, the snapshot family, sync-image, per-VM networking)
were the last operations Atlas drove as `boat <verb>` over SSH. These prove they
now go through the daemon over HTTP: the exact wire shape, the Task row that comes
out indistinguishable from an SSH run's, and — the seam that makes it invisible to
every call site — that `run_task` delegates a host verb to `run_boat_host_task`
with no caller changing. No daemon runs here; `requests.request` is patched, the
same way the lifecycle-client tests mock it.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import scripts_catalog
from atlas.atlas.boat_client import run_boat_host_read, run_boat_host_task
from atlas.atlas.ssh import run_probe, run_task
from atlas.tests import fixtures
from atlas.tests.test_boat_client import (
	REQUEST,
	_boat_host_token,
	_boat_server,
	_clear_virtual_machines,
	_operation,
	_Response,
)


class TestHostVerbCatalog(IntegrationTestCase):
	"""The set of host verbs Atlas routes over HTTP must agree with what the daemon
	serves — a verb in one and not the other is a 404 or a verb still on SSH."""

	def test_http_host_verbs_are_a_subset_of_the_boat_verbs(self) -> None:
		# Every HTTP host verb is a boat verb; the daemon binary implements it.
		self.assertTrue(scripts_catalog.HTTP_HOST_VERBS <= scripts_catalog.BOAT_ONLY_VERBS)

	def test_the_daemon_lifecycle_verbs_are_not_host_verbs(self) -> None:
		# bootstrap and reset-server bookend the daemon's existence; the two sweeps
		# are read-only and unjournaled. None is served over the host-verb endpoint.
		for verb in ("bootstrap", "reset-server", "poll-vm-traffic", "probe-woken-vms"):
			self.assertNotIn(verb, scripts_catalog.HTTP_HOST_VERBS)
			self.assertFalse(scripts_catalog.runs_on_boat_http(verb))

	def test_every_boat_host_verb_routes_over_http_except_bootstrap_and_reset(self) -> None:
		# Every host verb the boat binary implements now goes over HTTP — the whole
		# snapshot/provision/image/networking set — save the two that bookend the
		# daemon's own existence.
		for verb in scripts_catalog.BOAT_ONLY_VERBS:
			if verb in ("bootstrap", "reset-server", "poll-vm-traffic", "probe-woken-vms"):
				continue
			self.assertTrue(scripts_catalog.runs_on_boat_http(verb), verb)

	def test_the_heavier_verbs_route_over_http_now(self) -> None:
		# provision-vm, sync-image, warm-snapshot-vm and vm-tunnel moved off SSH once
		# their scoped grants landed in sudoers.d/boat.
		for verb in ("provision-vm", "sync-image", "warm-snapshot-vm", "vm-tunnel", "upload-snapshot-s3"):
			self.assertTrue(scripts_catalog.runs_on_boat_http(verb), verb)

	def test_bootstrap_and_reset_stay_on_ssh(self) -> None:
		# They install and tear down the daemon, so neither can be driven through it.
		for verb in ("bootstrap", "reset-server"):
			self.assertFalse(scripts_catalog.runs_on_boat_http(verb), verb)
			self.assertTrue(scripts_catalog.runs_on_boat(verb), verb)


class TestRunBoatHostTask(IntegrationTestCase):
	"""The Task row a host verb leaves behind, driven over HTTP."""

	def setUp(self) -> None:
		self.server = _boat_server()
		self.image = fixtures.make_image("boat-hostverb-image")
		_clear_virtual_machines()
		self.virtual_machine = fixtures.make_virtual_machine(self.server.name, self.image.name)

	def _run(self, script: str, variables: dict, response: _Response, virtual_machine: str | None = None):
		with _boat_host_token(self.server.name), patch(REQUEST, return_value=response) as request:
			task = run_boat_host_task(
				server=self.server.name,
				script=script,
				variables=variables,
				virtual_machine=virtual_machine,
				timeout_seconds=30,
			)
		return task, request

	def test_a_vm_scoped_verb_posts_to_the_host_verb_endpoint(self) -> None:
		operation = _operation(verb="snapshot-vm", output="+ lvcreate -s\n")
		task, request = self._run(
			"snapshot-vm",
			{"VIRTUAL_MACHINE_NAME": self.virtual_machine.name, "SNAPSHOT_ROOTFS_PATH": "/dev/atlas/vm"},
			_Response(payload=operation),
			virtual_machine=self.virtual_machine.name,
		)

		method, url = request.call_args.args
		self.assertEqual(method, "POST")
		self.assertTrue(url.endswith("/host-verbs/snapshot-vm"), url)
		body = request.call_args.kwargs["json"]
		self.assertEqual(body["operation_id"], task.name)
		# The whole variables dict rides in the body; the daemon renders it to flags.
		self.assertEqual(body["variables"]["SNAPSHOT_ROOTFS_PATH"], "/dev/atlas/vm")
		self.assertEqual(task.status, "Success")
		self.assertEqual(task.stdout, "+ lvcreate -s\n")
		self.assertEqual(task.script, "snapshot-vm")
		self.assertEqual(task.virtual_machine, self.virtual_machine.name)

	def test_a_typed_result_is_folded_onto_the_task_stdout(self) -> None:
		# snapshot-vm's size rides on the operation record's `result`; Atlas folds it
		# back onto stdout as the ATLAS_RESULT= line an SSH script would have printed,
		# so a caller parses one Task the same way whichever transport ran it.
		operation = _operation(verb="snapshot-vm", output="+ lvcreate -s\n", result={"size_bytes": 123})
		task, _request = self._run(
			"snapshot-vm",
			{"VIRTUAL_MACHINE_NAME": self.virtual_machine.name},
			_Response(payload=operation),
			virtual_machine=self.virtual_machine.name,
		)

		self.assertIn("ATLAS_RESULT=", task.stdout)
		self.assertIn("123", task.stdout)

	def test_a_host_scoped_verb_needs_no_virtual_machine(self) -> None:
		# sync-image names an image, not a VM, so the Task carries no VM link and the
		# body carries no VIRTUAL_MACHINE_NAME.
		task, request = self._run(
			"sync-image",
			{"IMAGE_NAME": self.image.name},
			_Response(payload=_operation(verb="sync-image", uuid="")),
		)

		self.assertTrue(request.call_args.args[1].endswith("/host-verbs/sync-image"))
		self.assertEqual(task.status, "Success")
		self.assertFalse(task.virtual_machine)

	def test_a_failed_operation_fails_the_task_and_raises(self) -> None:
		operation = _operation("Failure", verb="snapshot-vm", output="trace\n", error="thin pool is full")
		with (
			_boat_host_token(self.server.name),
			patch(REQUEST, return_value=_Response(payload=operation)),
			self.assertRaises(frappe.ValidationError) as raised,
		):
			run_boat_host_task(
				server=self.server.name,
				script="snapshot-vm",
				variables={"VIRTUAL_MACHINE_NAME": self.virtual_machine.name},
				virtual_machine=self.virtual_machine.name,
				timeout_seconds=30,
			)

		self.assertIn("thin pool is full", str(raised.exception))
		task = frappe.get_last_doc("Task", filters={"virtual_machine": self.virtual_machine.name})
		self.assertEqual(task.status, "Failure")


class TestRunTaskDelegatesHostVerbs(IntegrationTestCase):
	"""The seam that makes the transport invisible: `run_task` routes a host verb to
	the daemon and everything else to SSH, so no call site knows which it got."""

	def setUp(self) -> None:
		self.server = _boat_server()

	def test_run_task_delegates_an_http_host_verb_to_the_daemon(self) -> None:
		with patch("atlas.atlas.boat_client.run_boat_host_task") as delegated:
			run_task(
				server=self.server.name,
				script="snapshot-vm",
				variables={"VIRTUAL_MACHINE_NAME": "vm-1"},
				virtual_machine="vm-1",
				timeout_seconds=42,
			)

		delegated.assert_called_once()
		self.assertEqual(delegated.call_args.kwargs["script"], "snapshot-vm")
		self.assertEqual(delegated.call_args.kwargs["server"], self.server.name)
		self.assertEqual(delegated.call_args.kwargs["timeout_seconds"], 42)

	def test_run_task_does_not_delegate_a_verb_that_stays_on_ssh(self) -> None:
		# reset-server bookends the daemon's existence and keeps the SSH path, so
		# run_task must NOT route it to the daemon. It reaches the SSH runner, which we
		# stop at _execute_into so no real connection is opened.
		with (
			patch("atlas.atlas.boat_client.run_boat_host_task") as delegated,
			patch("atlas.atlas._ssh.runner._execute_into"),
			patch("atlas.atlas._ssh.runner.connection_for_server"),
		):
			run_task(server=self.server.name, script="reset-server", variables={}, timeout_seconds=30)

		delegated.assert_not_called()


class TestHostReadsOverHttp(IntegrationTestCase):
	"""The read-only sweeps move off run_probe's SSH onto the daemon's read path."""

	def setUp(self) -> None:
		self.server = _boat_server()

	def test_the_sweeps_route_over_http_and_stay_boat_verbs(self) -> None:
		for verb in ("poll-vm-traffic", "probe-woken-vms"):
			self.assertTrue(scripts_catalog.runs_on_boat_http_read(verb), verb)
			self.assertTrue(scripts_catalog.runs_on_boat(verb), verb)
		# A read is not a mutating host verb — the two sets never overlap.
		self.assertFalse(scripts_catalog.HTTP_HOST_READS & scripts_catalog.HTTP_HOST_VERBS)

	def test_run_boat_host_read_returns_the_daemons_output(self) -> None:
		with _boat_host_token(self.server.name), patch(
			REQUEST, return_value=_Response(payload={"output": 'ATLAS_RESULT={"woken": []}\n'})
		) as request:
			output = run_boat_host_read(
				script="probe-woken-vms", variables={"VMS_JSON": "[]"}, server=self.server.name
			)

		self.assertIn("ATLAS_RESULT=", output)
		self.assertTrue(request.call_args.args[1].endswith("/host-reads/probe-woken-vms"))

	def test_run_boat_host_read_never_raises(self) -> None:
		# run_probe's contract: a failed read logs and returns "", so a missed sweep
		# is one idle minute rather than a raised exception up the scheduler.
		with _boat_host_token(self.server.name), patch(
			REQUEST, return_value=_Response(status_code=500, payload={"error": "nft is unreachable"})
		):
			output = run_boat_host_read(
				script="poll-vm-traffic", variables={"VMS_JSON": "[]"}, server=self.server.name
			)
		self.assertEqual(output, "")

	def test_run_probe_delegates_a_read_verb_to_the_daemon(self) -> None:
		with patch("atlas.atlas.boat_client.run_boat_host_read", return_value="") as delegated:
			run_probe(server=self.server.name, script="poll-vm-traffic", variables={"VMS_JSON": "[]"})

		delegated.assert_called_once()
		self.assertEqual(delegated.call_args.kwargs["script"], "poll-vm-traffic")
