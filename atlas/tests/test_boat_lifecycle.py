"""Unit tests for the VM lifecycle over Boat — every verb driven by desired
state (spec/33 §2.4, §11.1, §11.3; WO-2).

No daemon runs here: `requests.request` is patched throughout, which is also the
point. These prove that each verb reaches its own endpoint under the Task's name,
that the desired spec and its fence epoch are stated BEFORE the verb that acts on
them, and — the rule this work order exists to get right — that a VM Atlas has
stated Stopped is not brought back by traffic, by the idle sweeper, or by an
enrolment that contradicts the stop.

The wire helpers and the host fixtures come from `test_boat_client`: all three
files cover the same seam, and a second `_Response` would be a second opinion
about what Boat's wire looks like.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.boat_client import (
	FIRST_BOOT_EPOCH,
	REBUILD_ANSWERED_ELSEWHERE,
	REBUILD_GUEST_FILES,
	REBUILD_IDENTITY,
	REBUILD_SOURCES,
	ROUTING_ENVIRONMENT_PATH,
	BoatClient,
	BoatError,
	desired_state,
	put_desired_state,
	rebuild_request,
	run_boat_task,
)
from atlas.atlas.doctype.virtual_machine import virtual_machine as virtual_machine_module
from atlas.atlas.task_results import parse_result
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

# A sleep task's typed result, in each transport's own shape. On SSH the script
# emits the `ATLAS_RESULT=` line; through Boat it is the `Operation.result` object
# `boat_client.OPERATION_RESULT_FIELD` names — which the contract does NOT carry
# yet, so `TestSleepWithoutATypedResult` covers what Atlas does today and these
# two cover the shape it is written against.
TYPED_RESULT = 'ATLAS_RESULT={"memory_snapshot": true}\n'
OPERATION_RESULT = {"memory_snapshot": True}

# One rebuild Task's variables, as `_rebuild_variables` states them for a VM with
# a data disk, a tenant and a Satellite: every field the mapping has a home for,
# alongside the ones the host answers for itself.
REBUILD_VARIABLES = {
	"VIRTUAL_MACHINE_NAME": "vm-1",
	"DISK_GB": "20",
	"VIRTUAL_MACHINE_IPV6": "2001:db8:b0a7::5",
	"SSH_PUBLIC_KEY": "ssh-ed25519 AAAA owner\nssh-ed25519 BBBB satellite",
	"ATLAS_FC_UID": "247312",
	"IPV4_HOST_CIDR": "100.64.0.1/30",
	"IPV4_GUEST_CIDR": "100.64.0.2/30",
	"IPV4_GATEWAY": "100.64.0.1",
	"PRIVATE_ADDRESS": "fdaa:1:2::7",
	"ROUTING_BASE_URL": "https://orchestrator.blr1.frappe.dev",
	"DATA_DISK_GB": "100",
	"DATA_DISK_FORMAT": "1",
	"DATA_DISK_MOUNT_AT": "/home",
	"IMAGE_NAME": "ubuntu-24-04",
}


def _sent(call) -> tuple[str, str, dict]:
	"""One recorded request as (method, url, body)."""
	method, url = call.args
	return method, url, call.kwargs["json"]


class TestBoatLifecycleWire(IntegrationTestCase):
	"""What Atlas puts on the wire for each WO-2 endpoint."""

	def setUp(self) -> None:
		self.client = BoatClient(base_url="http://198.51.100.7:8080/v1", token="s3cret")

	def test_every_verb_posts_its_own_endpoint_with_the_operation_identifier(self) -> None:
		# Rebuild is not here: it is the one verb whose request carries more than
		# the identifier, and `TestRebuildRequest` below is where that body lives.
		cases = (
			(self.client.pause_virtual_machine, "pause"),
			(self.client.resume_virtual_machine, "resume"),
			(self.client.sleep_virtual_machine, "sleep"),
			(self.client.wake_virtual_machine, "wake"),
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


class TestRebuildRequest(IntegrationTestCase):
	"""The one verb whose request is more than an identifier (spec/33 §2.4, §7.2).

	The rebuilt rootfs comes off the source's blocks, so everything that makes it
	THIS VM's has to be written back into it. A field that goes missing here is
	invisible until nobody can log in to the machine, which is why the mapping
	refuses a variable it does not know rather than dropping it."""

	def test_the_source_and_every_identity_field_reach_the_request(self) -> None:
		request = rebuild_request(REBUILD_VARIABLES)

		self.assertEqual(request["image"], "ubuntu-24-04")
		self.assertEqual(
			request["identity"],
			{
				"ipv6_address": "2001:db8:b0a7::5",
				"ipv4_guest_cidr": "100.64.0.2/30",
				"ipv4_gateway": "100.64.0.1",
				"private_address": "fdaa:1:2::7",
				"authorized_keys_blob": "ssh-ed25519 AAAA owner\nssh-ed25519 BBBB satellite",
				"data_disk_mount_at": "/home",
				"extra_env": [
					{
						"path": "/etc/atlas-routing.env",
						"content": "ATLAS_BASE_URL=https://orchestrator.blr1.frappe.dev\n",
					}
				],
			},
		)

	def test_the_routing_url_rides_as_an_anonymous_guest_file(self) -> None:
		"""§7.2's whole point: Boat's schema names no service-semantic field, so
		the routing config is a path and its bytes — byte-for-byte what the SSH
		path's `rootfs.py` writes — that the daemon cannot tell from any other."""
		identity = rebuild_request(REBUILD_VARIABLES)["identity"]

		self.assertNotIn("routing_base_url", identity)
		self.assertEqual(identity["extra_env"][0]["path"], ROUTING_ENVIRONMENT_PATH)
		self.assertEqual(
			identity["extra_env"][0]["content"], "ATLAS_BASE_URL=https://orchestrator.blr1.frappe.dev\n"
		)

	def test_an_atlas_with_no_satellite_writes_no_guest_file(self) -> None:
		identity = rebuild_request({**REBUILD_VARIABLES, "ROUTING_BASE_URL": ""})["identity"]
		self.assertNotIn("extra_env", identity)

	def test_a_restore_carries_both_snapshot_devices(self) -> None:
		variables = {
			**REBUILD_VARIABLES,
			"IMAGE_NAME": "",
			"SNAPSHOT_ROOTFS_PATH": "/dev/atlas/atlas-snap-s1",
			"DATA_SNAPSHOT_ROOTFS_PATH": "/dev/atlas/atlas-snap-s1-data",
		}
		request = rebuild_request(variables)

		self.assertEqual(request["snapshot_device"], "/dev/atlas/atlas-snap-s1")
		self.assertEqual(request["data_snapshot_device"], "/dev/atlas/atlas-snap-s1-data")
		# A rebuild from an image never states one: there is no image source for a
		# data disk, and wiping a tenant's home on an OS rebuild is not a default.
		self.assertNotIn("data_snapshot_device", rebuild_request(REBUILD_VARIABLES))

	def test_nothing_the_host_or_desired_state_answers_is_sent(self) -> None:
		"""The sizes are desired state, the uid is a host fact, and the host's end
		of the NAT44 /30 is host-side networking a rebuild does not touch. A verb
		that took a per-VM number off the wire when the store holds one could be
		asked to apply a shape the store disagrees with (spec/33 §2.4)."""
		body = json.dumps(rebuild_request(REBUILD_VARIABLES))

		for dropped in ("20", "247312", "100.64.0.1/30", "100"):
			self.assertNotIn(f'"{dropped}"', body)

	def test_a_variable_the_mapping_does_not_name_raises(self) -> None:
		# The failure this seam invites: a value that exists on one side and is
		# dropped on the other. Loud beats invisible.
		with self.assertRaises(BoatError) as raised:
			rebuild_request({**REBUILD_VARIABLES, "RESERVED_IPV4": "198.51.100.9"})

		self.assertIn("RESERVED_IPV4", str(raised.exception))

	def test_a_rebuild_with_no_source_is_refused(self) -> None:
		with self.assertRaises(BoatError) as raised:
			rebuild_request({**REBUILD_VARIABLES, "IMAGE_NAME": ""})

		self.assertIn("no source", str(raised.exception))

	def test_a_rebuild_with_no_authorized_keys_is_refused(self) -> None:
		# Boat's own CLI refuses this; Atlas is no more permissive. The VM would
		# boot, report success, and be unreachable forever.
		with self.assertRaises(BoatError) as raised:
			rebuild_request({**REBUILD_VARIABLES, "SSH_PUBLIC_KEY": ""})

		self.assertIn("no way back in", str(raised.exception))


class _BoatHostTestCase(IntegrationTestCase):
	"""One real-provider host with Boat switched on, and a VM placed on it."""

	def setUp(self) -> None:
		self.server = _boat_server()
		self.image = fixtures.make_image("boat-lifecycle-image")
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
			# Rebuild is the one verb with inputs of its own, and it refuses a
			# request that names no source, so it is given a real one.
			variables = {"VIRTUAL_MACHINE_NAME": self.virtual_machine.name}
			if verb == "rebuild-vm":
				variables = {**REBUILD_VARIABLES, **variables}
			with (
				self.subTest(verb=verb),
				_boat_host_token(self.server.name),
				patch(REQUEST, return_value=_Response(payload=payload)) as request,
			):
				task = run_boat_task(
					server=self.server.name,
					script=verb,
					variables=variables,
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
				operation = _operation(
					verb=f"{endpoint}-vm", uuid=virtual_machine.name, result=OPERATION_RESULT
				)

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


class TestRebuildThroughBoat(_BoatHostTestCase):
	"""A desk-driven rebuild, end to end: what `_rebuild_variables` states is what
	reaches the host, and the two transports lay down the same guest."""

	def _rebuild(self, virtual_machine, *arguments) -> dict:
		"""Drive one rebuild and return the body of the POST that carried it."""
		operation = _operation(verb="rebuild-vm", uuid=virtual_machine.name)
		_result, calls = self._drive(lambda: virtual_machine.rebuild(*arguments), {}, operation)
		return _sent(calls[1])[2]

	def _loaded(self) -> "frappe.model.document.Document":
		"""A VM carrying everything a rebuild has to write back: a tenant (so it is
		on the private plane), a mounted data disk, and a Satellite to route to."""
		if not frappe.db.exists("Tenant", "boat-rebuild-team"):
			frappe.get_doc({"doctype": "Tenant", "team": "boat-rebuild-team"}).insert(ignore_permissions=True)
		frappe.db.set_single_value(
			"Atlas Settings", "satellite_routing_base_url", "https://orchestrator.blr1.frappe.dev"
		)
		return self._fresh(
			status="Stopped",
			tenant="boat-rebuild-team",
			data_disk_gigabytes=100,
			data_disk_format_and_mount=1,
			data_disk_mount_point="/home",
		)

	def _snapshot_of(self, virtual_machine, title: str) -> str:
		"""An Available snapshot of `virtual_machine`, root and data disk both."""
		snapshot = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": title,
				"virtual_machine": virtual_machine.name,
				"server": virtual_machine.server,
				"status": "Available",
				"rootfs_path": "/dev/atlas/atlas-snap-s1",
				"data_rootfs_path": "/dev/atlas/atlas-snap-s1-data",
			}
		).insert(ignore_permissions=True)
		return snapshot.name

	def test_a_rebuild_from_an_image_carries_the_source_and_the_identity(self) -> None:
		virtual_machine = self._loaded()

		body = self._rebuild(virtual_machine, "image")

		self.assertEqual(body["image"], self.image.name)
		identity = body["identity"]
		self.assertEqual(identity["ipv6_address"], virtual_machine.ipv6_address)
		self.assertEqual(identity["authorized_keys_blob"], virtual_machine.ssh_public_key)
		self.assertEqual(identity["data_disk_mount_at"], "/home")
		# The two the fresh rootfs would otherwise lose outright: a rebuilt VM off
		# the private plane, and a bench VM that can no longer route its own sites.
		self.assertTrue(identity["private_address"].startswith("fdaa:"))
		self.assertEqual(
			identity["extra_env"],
			[
				{
					"path": ROUTING_ENVIRONMENT_PATH,
					"content": "ATLAS_BASE_URL=https://orchestrator.blr1.frappe.dev\n",
				}
			],
		)

	def test_a_rebuild_from_a_snapshot_carries_the_devices_and_no_image(self) -> None:
		virtual_machine = self._fresh(status="Stopped")
		snapshot = self._snapshot_of(virtual_machine, "snap")

		body = self._rebuild(virtual_machine, "snapshot", snapshot)

		self.assertEqual(body["snapshot_device"], "/dev/atlas/atlas-snap-s1")
		self.assertEqual(body["data_snapshot_device"], "/dev/atlas/atlas-snap-s1-data")
		self.assertNotIn("image", body)

	def test_every_rebuild_variable_has_a_home_on_the_wire(self) -> None:
		"""The guard the mapping exists for. A variable added to the SSH path's
		dict and not to the tables would otherwise be dropped in silence — this
		fails the moment the two sides stop agreeing on the same list."""
		virtual_machine = self._loaded()
		snapshot = self._snapshot_of(virtual_machine, "mapping-snap")
		mapped = set(REBUILD_SOURCES) | set(REBUILD_IDENTITY) | REBUILD_GUEST_FILES
		mapped |= REBUILD_ANSWERED_ELSEWHERE

		for source_type, source in (("image", None), ("snapshot", snapshot)):
			stated = set(virtual_machine._rebuild_variables(source_type, source))
			self.assertEqual(stated - mapped, set(), f"unmapped {source_type} rebuild variables")


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


class TestSleepWithoutATypedResult(_BoatHostTestCase):
	"""`sleep` is the only verb routed through Boat that reads structured output,
	and Boat's `Operation` has nowhere to put it: `output`, `error`, `exit_code`
	and nothing else (`api/openapi.yaml`). Boat computes exactly the value Atlas
	wants — `SleepResult.MemorySnapshot` — and discards it.

	Insisting on the line was the defect, and it was quiet in every direction: the
	VM parked on its host, the Task row committed `Success`, `self.status =
	"Sleeping"` never ran, and `sleep_idle_vms` swallowed the throw. The row stayed
	`Running`, so the next minute's sweep slept it again, with a fresh `op_id` that
	made Boat genuinely re-run the verb — forever. Meanwhile `capacity_for_server`
	excludes only `Sleeping` rows from the RAM sum, so the RAM the feature exists to
	free was never booked back on exactly the hosts being cut over."""

	def _sleepy(self, **fields) -> "frappe.model.document.Document":
		return self._fresh(status="Running", sleep_on_idle=1, idle_timeout_seconds=300, **fields)

	def _sleep(self, virtual_machine, **operation) -> tuple[object, list]:
		return self._drive(
			virtual_machine.sleep, {}, _operation(verb="sleep-vm", uuid=virtual_machine.name, **operation)
		)

	def test_a_sleep_that_cannot_learn_the_snapshot_state_still_parks_the_row(self) -> None:
		virtual_machine = self._sleepy()

		self._sleep(virtual_machine, output="parked vm\n")

		virtual_machine.reload()
		self.assertEqual(virtual_machine.status, "Sleeping")
		self.assertIsNotNone(virtual_machine.last_stopped)

	def test_the_idle_sweeper_sleeps_an_idle_vm_exactly_once(self) -> None:
		"""The consequence that made it expensive rather than cosmetic: a row left
		`Running` is re-selected by the next tick, one Task per minute per idle VM,
		each one a real verb on the host."""
		virtual_machine = self._sleepy(last_traffic_at="2020-01-01 00:00:00")
		operation = _operation(verb="sleep-vm", uuid=virtual_machine.name, output="parked vm\n")

		with _boat_host_token(self.server.name), patch(REQUEST, return_value=_Response(payload=operation)):
			virtual_machine_module.sleep_idle_vms()
			virtual_machine.reload()
			self.assertEqual(virtual_machine.status, "Sleeping")

			virtual_machine_module.sleep_idle_vms()

		self.assertEqual(
			frappe.db.count("Task", {"virtual_machine": virtual_machine.name, "script": "sleep-vm"}), 1
		)

	def test_a_typed_result_is_read_the_moment_boat_carries_one(self) -> None:
		"""Atlas is written against the field it needs (`Operation.result`), so the
		day the contract carries it nothing else has to change — and every OTHER call
		site that parses a verb's stdout reads it the same way, which is what stops
		the next verb repeating this the day it gains a Boat endpoint."""
		virtual_machine = self._sleepy()

		self._sleep(virtual_machine, output="parked vm\n", result=OPERATION_RESULT)

		virtual_machine.reload()
		self.assertEqual(virtual_machine.status, "Sleeping")
		self.assertTrue(virtual_machine.has_memory_snapshot)

	def test_the_typed_result_lands_on_the_task_as_the_line_ssh_would_have_written(self) -> None:
		# One Task shape, whichever transport filled it: the operator still reads the
		# trace, and `task_results.parse_result` still finds its line.
		virtual_machine = self._sleepy()
		operation = _operation(
			verb="sleep-vm",
			uuid=virtual_machine.name,
			output="parked vm\n",
			result={"memory_snapshot": False},
		)

		with _boat_host_token(self.server.name), patch(REQUEST, return_value=_Response(payload=operation)):
			task_name = virtual_machine.sleep()

		stdout = frappe.db.get_value("Task", task_name, "stdout")
		self.assertIn("parked vm", stdout)
		self.assertEqual(parse_result(stdout), {"memory_snapshot": False})

	def test_any_verb_that_gains_a_result_is_read_the_same_way(self) -> None:
		"""What keeps the defect from repeating. `sleep` is the only Boat-routed verb
		that parses structured output today — every other such call site (`snapshot`,
		`warm-snapshot`, the migration phases, the bootstrap facts) holds `run_task`
		directly — and the latent risk is the day one of them gains a Boat endpoint.

		So the fix lives in the TRANSPORT, not in the call site: a typed result
		becomes the same `ATLAS_RESULT=` line on the Task row for every verb, and
		`parse_result` reads one Task the same way whichever transport filled it."""
		verbs = [verb for verb in VERB_ENDPOINTS if verb != "rebuild-vm"]
		for verb in verbs:
			payload = _operation(
				verb=verb, uuid=self.virtual_machine.name, output="trace\n", result={"size_bytes": 42}
			)
			with (
				self.subTest(verb=verb),
				_boat_host_token(self.server.name),
				patch(REQUEST, return_value=_Response(payload=payload)),
			):
				task = run_boat_task(
					server=self.server.name,
					script=verb,
					variables={"VIRTUAL_MACHINE_NAME": self.virtual_machine.name},
					virtual_machine=self.virtual_machine.name,
					timeout_seconds=30,
				)

				self.assertEqual(parse_result(task.stdout), {"size_bytes": 42})


class TestFakeHostIsNeverCalled(IntegrationTestCase):
	"""A Fake-backed host never gets a Boat call, exactly as it never gets an SSH
	connection — the dev/test fleet is Fake hosts, so this is what lets them run
	the same code paths a real host does."""

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
