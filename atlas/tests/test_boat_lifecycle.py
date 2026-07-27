"""Unit tests for the VM lifecycle over Boat — every verb driven by desired
state (spec/33 §2.4, §11.1, §11.3; WO-2).

No daemon runs here: `requests.request` is patched throughout, which is also the
point. These prove that each verb reaches its own endpoint under the Task's name,
that the desired spec and its fence epoch are stated BEFORE the verb that acts on
them, that a host without `Server.boat_enabled` takes the same SSH path it always
took, and — the rule this work order exists to get right — that a VM Atlas has
stated Stopped is not brought back by traffic, by the idle sweeper, or by an
enrolment that contradicts the stop.

The wire helpers and the host fixtures come from `test_boat_client`: all three
files cover the same seam, and a second `_Response` would be a second opinion
about what Boat's wire looks like.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.boat_client import (
	FIRST_BOOT_EPOCH,
	BoatClient,
	BoatError,
	desired_state,
	put_desired_state,
	run_boat_task,
)
from atlas.atlas.doctype.virtual_machine import virtual_machine as virtual_machine_module
from atlas.tests import fixtures
from atlas.tests._mocks import fake_task
from atlas.tests.test_boat_client import (
	REQUEST,
	_boat_host_token,
	_boat_server,
	_clear_virtual_machines,
	_operation,
	_Response,
)

# Task verb -> the endpoint that serves it. The whole routing table, restated
# here so a verb that quietly moves to another endpoint fails a test rather than
# a host.
VERB_ENDPOINTS = {
	"start-vm": "start",
	"stop-vm": "stop",
	"pause-vm": "pause",
	"resume-vm": "resume",
	"sleep-vm": "sleep",
	"wake-vm": "wake",
	"rebuild-vm": "rebuild",
	"terminate-vm": "terminate",
	"resize-vm": "resize",
}

# Every operation in these tests carries the typed result line, because a verb's
# result is read the same way on either transport: `sleep()` parses
# `memory_snapshot` out of the daemon's `output` exactly as it parses it out of
# an SSH task's stdout, which is what the contract's "the same text a Task row's
# stdout carries today" has to mean (spec/33 §2.4).
TYPED_RESULT = 'ATLAS_RESULT={"memory_snapshot": true}\n'


def _sent(call) -> tuple[str, str, dict]:
	"""One recorded request as (method, url, body)."""
	method, url = call.args
	return method, url, call.kwargs["json"]


class TestBoatLifecycleWire(IntegrationTestCase):
	"""What Atlas puts on the wire for each WO-2 endpoint."""

	def setUp(self) -> None:
		self.client = BoatClient(base_url="http://198.51.100.7:8080/v1", token="s3cret")

	def test_every_verb_posts_its_own_endpoint_with_the_operation_identifier(self) -> None:
		cases = (
			(self.client.pause_virtual_machine, "pause"),
			(self.client.resume_virtual_machine, "resume"),
			(self.client.sleep_virtual_machine, "sleep"),
			(self.client.wake_virtual_machine, "wake"),
			(self.client.rebuild_virtual_machine, "rebuild"),
			(self.client.terminate_virtual_machine, "terminate"),
			(self.client.resize_virtual_machine, "resize"),
		)
		for call, endpoint in cases:
			payload = _operation(verb=f"{endpoint}-vm")
			with (
				self.subTest(endpoint=endpoint),
				patch(REQUEST, return_value=_Response(payload=payload)) as request,
			):
				call("vm-1", operation_id="task-boat-9")

				method, url, body = _sent(request.call_args)
				self.assertEqual(method, "POST")
				self.assertEqual(url, f"http://198.51.100.7:8080/v1/vms/vm-1/{endpoint}")
				# These verbs carry nothing else: their arguments are desired
				# state, and the PUT stated it.
				self.assertEqual(body, {"operation_id": "task-boat-9"})

	def test_put_desired_states_the_spec_and_its_fence_epoch(self) -> None:
		spec = {"boot_epoch": 4, "desired_power": "Running", "vcpus": 2, "memory_megabytes": 2048}
		with patch(REQUEST, return_value=_Response(payload=spec)) as request:
			self.client.put_desired("vm-1", spec)

		method, url, body = _sent(request.call_args)
		self.assertEqual(method, "PUT")
		self.assertEqual(url, "http://198.51.100.7:8080/v1/vms/vm-1")
		self.assertEqual(body, {**spec, "uuid": "vm-1"})

	def test_the_document_names_the_vm_the_path_names(self) -> None:
		# A request that named two VMs would have two answers.
		with patch(REQUEST, return_value=_Response(payload={})) as request:
			self.client.put_desired("vm-1", {"uuid": "vm-other", "boot_epoch": 1, "desired_power": "Stopped"})

		self.assertEqual(_sent(request.call_args)[2]["uuid"], "vm-1")

	def test_a_refused_verb_raises_with_the_daemons_own_sentence(self) -> None:
		refusal = _Response(
			status_code=409, payload={"error": "no fence held for this VM", "reason": "no-fence"}
		)
		with patch(REQUEST, return_value=refusal), self.assertRaises(BoatError) as raised:
			self.client.wake_virtual_machine("vm-1", operation_id="task-boat-9")

		self.assertIn("no fence held for this VM", str(raised.exception))


class _BoatHostTestCase(IntegrationTestCase):
	"""One real-provider host with Boat switched on, and a VM placed on it."""

	def setUp(self) -> None:
		self.server = _boat_server()
		self.image = fixtures.make_image("boat-lifecycle-image")
		frappe.db.set_value("Server", self.server.name, "boat_enabled", 1, update_modified=False)
		self.virtual_machine = self._fresh()

	def _fresh(self, **fields) -> "frappe.model.document.Document":
		"""A new VM on this host, in the state one verb runs from."""
		_clear_virtual_machines()
		virtual_machine = fixtures.make_virtual_machine(self.server.name, self.image.name)
		if fields:
			frappe.db.set_value("Virtual Machine", virtual_machine.name, fields, update_modified=False)
			virtual_machine.reload()
		return virtual_machine

	def _drive(self, action, *payloads) -> tuple[object, list]:
		"""Run one lifecycle call with Boat's answers queued in order, and return
		its result alongside every request that reached the wire."""
		answers = [_Response(payload=payload) for payload in payloads]
		with _boat_host_token(self.server.name), patch(REQUEST, side_effect=answers) as request:
			result = action()
		return result, request.call_args_list


class TestDesiredStateDocument(_BoatHostTestCase):
	"""The `DesiredVirtualMachine` document Atlas builds from a VM row."""

	def test_it_carries_the_fence_epoch_and_the_desired_spec(self) -> None:
		self.virtual_machine.desired_power = "Running"
		desired = desired_state(self.virtual_machine)

		self.assertEqual(desired["uuid"], self.virtual_machine.name)
		self.assertEqual(desired["boot_epoch"], FIRST_BOOT_EPOCH)
		self.assertEqual(desired["desired_power"], "Running")
		self.assertEqual(desired["vcpus"], self.virtual_machine.vcpus)
		self.assertEqual(desired["memory_megabytes"], self.virtual_machine.memory_megabytes)
		self.assertEqual(desired["ipv6_address"], self.virtual_machine.ipv6_address)
		self.assertEqual(desired["mac_address"], self.virtual_machine.mac_address)

	def test_it_states_no_observation(self) -> None:
		# Boat reports fact; stating it back would make Atlas an authority on
		# something it does not hold (spec/33 §1).
		self.virtual_machine.desired_power = "Running"
		desired = desired_state(self.virtual_machine)

		for observed in ("status", "observed_status", "has_memory_snapshot", "last_started"):
			self.assertNotIn(observed, desired)

	def test_a_resize_states_numbers_the_row_does_not_carry_yet(self) -> None:
		self.virtual_machine.desired_power = "Stopped"
		desired = desired_state(self.virtual_machine, memory_megabytes=4096)

		self.assertEqual(desired["memory_megabytes"], 4096)
		self.assertEqual(self.virtual_machine.memory_megabytes, 512)

	def test_a_row_with_no_stated_power_raises_rather_than_guessing(self) -> None:
		with self.assertRaises(BoatError) as raised:
			desired_state(self.virtual_machine)

		self.assertIn("desired_power", str(raised.exception))

	def test_a_stopped_vm_is_not_enrolled_in_the_sleep_reflex(self) -> None:
		# The precedence rule, stated rather than left for the daemon to resolve.
		self.virtual_machine.sleep_on_idle = 1
		self.virtual_machine.desired_power = "Stopped"
		self.assertFalse(desired_state(self.virtual_machine)["sleep_on_idle"])

		self.virtual_machine.desired_power = "Running"
		self.assertTrue(desired_state(self.virtual_machine)["sleep_on_idle"])


class TestBoatVerbRouting(_BoatHostTestCase):
	"""Each Task verb reaches its own endpoint, under the Task's name."""

	def test_each_verb_routes_to_its_endpoint_as_the_task(self) -> None:
		for verb, endpoint in VERB_ENDPOINTS.items():
			payload = _operation(verb=verb, uuid=self.virtual_machine.name)
			with (
				self.subTest(verb=verb),
				_boat_host_token(self.server.name),
				patch(REQUEST, return_value=_Response(payload=payload)) as request,
			):
				task = run_boat_task(
					server=self.server.name,
					script=verb,
					variables={"VIRTUAL_MACHINE_NAME": self.virtual_machine.name},
					virtual_machine=self.virtual_machine.name,
					timeout_seconds=30,
				)

				_method, url, body = _sent(request.call_args)
				self.assertTrue(url.endswith(f"/vms/{self.virtual_machine.name}/{endpoint}"), url)
				# The identity that makes a retry a replay rather than a second run.
				self.assertEqual(body["operation_id"], task.name)


def _lifecycle_cases() -> tuple:
	"""Every lifecycle verb: the state it runs from, the call, the endpoint it
	must reach, and the power that call states."""
	return (
		("start", {"status": "Stopped"}, lambda machine: machine.start(), "start", "Running"),
		("stop", {"status": "Running"}, lambda machine: machine.stop(), "stop", "Stopped"),
		("pause", {"status": "Running"}, lambda machine: machine.pause(), "pause", "Running"),
		("resume", {"status": "Paused"}, lambda machine: machine.resume(), "resume", "Running"),
		(
			"sleep",
			{"status": "Running", "sleep_on_idle": 1, "idle_timeout_seconds": 300},
			lambda machine: machine.sleep(),
			"sleep",
			# Sleeping satisfies Running: the VM is parked and wakeable, not off.
			"Running",
		),
		("wake", {"status": "Sleeping"}, lambda machine: machine.wake(), "wake", "Running"),
		("rebuild", {"status": "Stopped"}, lambda machine: machine.rebuild("image"), "rebuild", "Stopped"),
		(
			"resize",
			{"status": "Stopped"},
			lambda machine: machine.resize(memory_megabytes=1024),
			"resize",
			"Stopped",
		),
		("terminate", {"status": "Running"}, lambda machine: machine.terminate(), "terminate", "Stopped"),
	)


class TestLifecycleThroughBoat(_BoatHostTestCase):
	"""Every lifecycle method on a Boat host: state the intent, then ask for it."""

	def test_every_verb_states_its_intent_before_it_acts(self) -> None:
		for label, fields, act, endpoint, power in _lifecycle_cases():
			with self.subTest(verb=label):
				virtual_machine = self._fresh(**fields)
				operation = _operation(verb=f"{endpoint}-vm", uuid=virtual_machine.name, output=TYPED_RESULT)

				_result, calls = self._drive(lambda: act(virtual_machine), {}, operation)

				# Two calls, in this order: the mutation, then "now".
				self.assertEqual(len(calls), 2)
				put_method, put_url, desired = _sent(calls[0])
				self.assertEqual(put_method, "PUT")
				self.assertTrue(put_url.endswith(f"/vms/{virtual_machine.name}"), put_url)
				self.assertEqual(desired["desired_power"], power)
				self.assertEqual(desired["boot_epoch"], FIRST_BOOT_EPOCH)

				post_method, post_url, body = _sent(calls[1])
				self.assertEqual(post_method, "POST")
				self.assertTrue(post_url.endswith(f"/vms/{virtual_machine.name}/{endpoint}"), post_url)
				self.assertIn("operation_id", body)

				virtual_machine.reload()
				self.assertEqual(virtual_machine.desired_power, power)

	def test_a_resize_states_the_new_numbers_before_asking_for_them(self) -> None:
		# Boat's resize carries no numbers — it applies the desired ones — so the
		# PUT is the only place they are stated.
		virtual_machine = self._fresh(status="Stopped")
		operation = _operation(verb="resize-vm", uuid=virtual_machine.name)
		# Room on the CPU axis, so the capacity gate is not what this test proves.
		frappe.db.set_value("Server", self.server.name, "vcpus_total", 32, update_modified=False)

		_result, calls = self._drive(
			lambda: virtual_machine.resize(vcpus=4, memory_megabytes=4096, disk_gigabytes=20), {}, operation
		)

		desired = _sent(calls[0])[2]
		self.assertEqual(desired["vcpus"], 4)
		self.assertEqual(desired["memory_megabytes"], 4096)
		self.assertEqual(desired["disk_gigabytes"], 20)

	def test_the_first_assertion_issues_epoch_one(self) -> None:
		virtual_machine = self._fresh(status="Stopped")
		self.assertFalse(virtual_machine.boot_epoch)

		_result, calls = self._drive(virtual_machine.start, {}, _operation(uuid=virtual_machine.name))

		self.assertEqual(_sent(calls[0])[2]["boot_epoch"], 1)
		virtual_machine.reload()
		self.assertEqual(virtual_machine.boot_epoch, 1)

	def test_no_verb_ever_bumps_the_epoch(self) -> None:
		# The epoch bumps at exactly one point — a migration's repoint — and this
		# work order contains none of them.
		virtual_machine = self._fresh(status="Stopped", boot_epoch=7)

		_result, calls = self._drive(virtual_machine.start, {}, _operation(uuid=virtual_machine.name))
		self.assertEqual(_sent(calls[0])[2]["boot_epoch"], 7)

		_result, calls = self._drive(
			virtual_machine.stop, {}, _operation(verb="stop-vm", uuid=virtual_machine.name)
		)
		self.assertEqual(_sent(calls[0])[2]["boot_epoch"], 7)

		virtual_machine.reload()
		self.assertEqual(virtual_machine.boot_epoch, 7)

	def test_provision_enrols_the_vm_with_its_host(self) -> None:
		# A Boat refuses to boot a UUID it holds no fence for, so the VM is fenced
		# where it comes to exist. Provision itself has no Boat verb and stays SSH.
		virtual_machine = self._fresh()
		with (
			_boat_host_token(self.server.name),
			patch.object(virtual_machine_module, "run_task", return_value=fake_task("task-ssh-1")),
			patch(REQUEST, return_value=_Response(payload={})) as request,
		):
			virtual_machine.provision()

		method, _url, desired = _sent(request.call_args)
		self.assertEqual(method, "PUT")
		self.assertEqual(desired["desired_power"], "Running")
		self.assertEqual(desired["boot_epoch"], FIRST_BOOT_EPOCH)

	def test_a_memory_snapshot_stop_states_stopped_and_stays_on_ssh(self) -> None:
		# Boat serves no snapshot-stop verb. The intent still has to be stated, or
		# the reconciler boots the VM again the moment the unit goes down.
		virtual_machine = self._fresh(status="Running")
		snapshot_task = fake_task("task-snap-1", stdout=TYPED_RESULT)
		with (
			_boat_host_token(self.server.name),
			patch.object(virtual_machine_module, "run_task", return_value=snapshot_task) as run_task,
			patch.object(virtual_machine_module, "run_boat_task") as run_boat,
			patch(REQUEST, return_value=_Response(payload={})) as request,
		):
			virtual_machine.stop(memory_snapshot=True)

		run_boat.assert_not_called()
		self.assertEqual(run_task.call_args.kwargs["script"], "snapshot-stop-vm")
		method, _url, desired = _sent(request.call_args)
		self.assertEqual(method, "PUT")
		self.assertEqual(desired["desired_power"], "Stopped")

	def test_re_asserting_desired_state_states_it_again_and_changes_nothing_else(self) -> None:
		virtual_machine = self._fresh(status="Running")

		_result, calls = self._drive(virtual_machine.assert_desired_state, {})

		self.assertEqual(len(calls), 1)
		method, _url, desired = _sent(calls[0])
		self.assertEqual(method, "PUT")
		# No power stated, so the one the status implies is used.
		self.assertEqual(desired["desired_power"], "Running")
		virtual_machine.reload()
		self.assertEqual(virtual_machine.status, "Running")


class TestBoatFailuresAreLoud(_BoatHostTestCase):
	"""A failure at this boundary raises. It never finds another way onto the host."""

	def test_a_failing_verb_raises_and_never_falls_back_to_ssh(self) -> None:
		virtual_machine = self._fresh(status="Stopped")
		answers = [_Response(payload={}), _Response(status_code=500, payload={"error": "thin pool is full"})]
		with (
			_boat_host_token(self.server.name),
			patch(REQUEST, side_effect=answers),
			patch("atlas.atlas._ssh.runner.run_ssh") as run_ssh,
			self.assertRaises(frappe.ValidationError) as raised,
		):
			virtual_machine.start()

		self.assertIn("thin pool is full", str(raised.exception))
		run_ssh.assert_not_called()
		virtual_machine.reload()
		self.assertEqual(virtual_machine.status, "Stopped")

	def test_a_refused_desired_state_stops_the_verb_before_it_runs(self) -> None:
		virtual_machine = self._fresh(status="Stopped")
		refusal = _Response(status_code=409, payload={"error": "epoch 1 is older than the fence held"})
		with (
			_boat_host_token(self.server.name),
			patch(REQUEST, return_value=refusal) as request,
			self.assertRaises(frappe.ValidationError) as raised,
		):
			virtual_machine.start()

		self.assertIn("older than the fence held", str(raised.exception))
		# The PUT, and nothing after it: a host that did not take the intent is
		# never asked to act on it.
		self.assertEqual(request.call_count, 1)
		virtual_machine.reload()
		self.assertEqual(virtual_machine.status, "Stopped")


class TestStopOutranksWake(_BoatHostTestCase):
	"""`desired_power = Stopped` is what a stop states, and nothing walks it back
	except an operator saying so (spec/33 §11.3)."""

	def test_a_stop_states_stopped_and_disarms_the_sleep_reflex(self) -> None:
		virtual_machine = self._fresh(status="Running", sleep_on_idle=1, idle_timeout_seconds=300)

		_result, calls = self._drive(
			virtual_machine.stop, {}, _operation(verb="stop-vm", uuid=virtual_machine.name)
		)

		desired = _sent(calls[0])[2]
		self.assertEqual(desired["desired_power"], "Stopped")
		self.assertFalse(desired["sleep_on_idle"])
		virtual_machine.reload()
		self.assertEqual(virtual_machine.desired_power, "Stopped")
		# The enrolment is unchanged on the row: the next start states it again.
		self.assertTrue(virtual_machine.sleep_on_idle)

	def test_a_stopped_vm_cannot_be_put_to_sleep(self) -> None:
		virtual_machine = self._fresh(
			status="Running", sleep_on_idle=1, idle_timeout_seconds=300, desired_power="Stopped"
		)

		with patch(REQUEST) as request, self.assertRaises(frappe.ValidationError) as raised:
			virtual_machine.sleep()

		self.assertIn("stopped by intent", str(raised.exception))
		request.assert_not_called()

	def test_the_idle_sweeper_leaves_a_stopped_vm_alone(self) -> None:
		# sleep_idle_vms swallows the refusal, so what this proves is that the
		# sweeper cannot park a VM the operator stopped.
		virtual_machine = self._fresh(
			status="Running",
			sleep_on_idle=1,
			idle_timeout_seconds=120,
			desired_power="Stopped",
			last_traffic_at="2020-01-01 00:00:00",
		)

		with patch(REQUEST) as request:
			virtual_machine_module.sleep_idle_vms()

		request.assert_not_called()
		virtual_machine.reload()
		self.assertEqual(virtual_machine.status, "Running")

	def test_traffic_does_not_wake_a_stopped_vm(self) -> None:
		virtual_machine = self._fresh(status="Sleeping", desired_power="Stopped")

		virtual_machine_module._adopt_wake(virtual_machine.name, frappe.utils.now_datetime())

		virtual_machine.reload()
		self.assertEqual(virtual_machine.status, "Sleeping")

	def test_traffic_still_wakes_a_vm_nobody_stopped(self) -> None:
		virtual_machine = self._fresh(status="Sleeping", desired_power="Running")

		virtual_machine_module._adopt_wake(virtual_machine.name, frappe.utils.now_datetime())

		virtual_machine.reload()
		self.assertEqual(virtual_machine.status, "Running")

	def test_an_operator_wake_states_running(self) -> None:
		# The one thing allowed to reverse a stop is somebody saying so.
		virtual_machine = self._fresh(status="Sleeping", desired_power="Stopped")

		_result, calls = self._drive(
			virtual_machine.wake, {}, _operation(verb="wake-vm", uuid=virtual_machine.name)
		)

		self.assertEqual(_sent(calls[0])[2]["desired_power"], "Running")
		virtual_machine.reload()
		self.assertEqual(virtual_machine.desired_power, "Running")


class TestFlagOffChangesNothing(IntegrationTestCase):
	"""`boat_enabled` clear is the rollback: every verb runs the call it ran
	before, no desired state is stated, and no request is made at all."""

	def setUp(self) -> None:
		self.server = _boat_server()
		self.image = fixtures.make_image("boat-lifecycle-image")
		frappe.db.set_value("Server", self.server.name, "boat_enabled", 0, update_modified=False)

	def _fresh(self, **fields) -> "frappe.model.document.Document":
		_clear_virtual_machines()
		virtual_machine = fixtures.make_virtual_machine(self.server.name, self.image.name)
		frappe.db.set_value("Virtual Machine", virtual_machine.name, fields, update_modified=False)
		virtual_machine.reload()
		return virtual_machine

	def test_every_verb_stays_on_the_ssh_path(self) -> None:
		for label, fields, act, endpoint, _power in _lifecycle_cases():
			with self.subTest(verb=label):
				virtual_machine = self._fresh(**fields)
				task = fake_task(f"task-ssh-{label}", stdout=TYPED_RESULT)
				with (
					patch.object(virtual_machine_module, "run_task", return_value=task) as run_task,
					patch.object(virtual_machine_module, "run_boat_task") as run_boat,
					patch(REQUEST) as request,
				):
					act(virtual_machine)

				run_boat.assert_not_called()
				request.assert_not_called()
				self.assertEqual(run_task.call_args.kwargs["script"], f"{endpoint}-vm")
				self.assertEqual(run_task.call_args.kwargs["server"], self.server.name)
				virtual_machine.reload()
				# Nothing states intent off a Boat host: `status` is still the
				# whole operator surface, exactly as before WO-2.
				self.assertFalse(virtual_machine.desired_power)
				self.assertFalse(virtual_machine.boot_epoch)

	def test_re_asserting_desired_state_is_refused_off_a_boat_host(self) -> None:
		virtual_machine = self._fresh(status="Running")
		with patch(REQUEST) as request, self.assertRaises(frappe.ValidationError):
			virtual_machine.assert_desired_state()

		request.assert_not_called()


class TestFakeHostIsNeverCalled(IntegrationTestCase):
	"""A Fake-backed host never gets a Boat call, exactly as it never gets an SSH
	connection — the dev/test fleet is Fake hosts, so this is what keeps
	`boat_enabled` a no-op there."""

	def setUp(self) -> None:
		provider = fixtures.make_provider_row("boat-fake-lifecycle-provider", provider_type="Fake")
		fixtures.set_atlas_settings(provider)
		self.server = fixtures.make_server(
			provider,
			"boat-fake-lifecycle-server",
			ipv4_address="203.0.113.45",
			ipv6_address="2001:db8:fa4f::1",
			ipv6_prefix="2001:db8:fa4f::/64",
			ipv6_virtual_machine_range="2001:db8:fa4f::/124",
			status="Active",
			boat_enabled=1,
		)
		self.image = fixtures.make_image("boat-fake-lifecycle-image")
		_clear_virtual_machines()
		self.virtual_machine = fixtures.make_virtual_machine(self.server.name, self.image.name)
		frappe.db.set_value(
			"Virtual Machine", self.virtual_machine.name, "status", "Stopped", update_modified=False
		)
		self.virtual_machine.reload()

	def test_the_intent_is_recorded_and_nothing_is_called(self) -> None:
		with patch(REQUEST) as request:
			self.virtual_machine.start()

		request.assert_not_called()
		self.virtual_machine.reload()
		# The row still records what Atlas wants — there is simply no daemon to
		# tell, so the fence lives only in Atlas's books.
		self.assertEqual(self.virtual_machine.desired_power, "Running")
		self.assertEqual(self.virtual_machine.boot_epoch, FIRST_BOOT_EPOCH)
		self.assertEqual(self.virtual_machine.status, "Running")

	def test_stating_desired_state_needs_no_credentials(self) -> None:
		self.virtual_machine.desired_power = "Running"
		with patch(REQUEST) as request:
			self.assertEqual(put_desired_state(self.virtual_machine), {})

		request.assert_not_called()
