from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.api.models import VirtualMachineDetailResponse, VirtualMachineResponse
from atlas.api.routes.virtual_machines import (
	attach_virtual_machine_ip_address,
	create_virtual_machine,
	delete_virtual_machine,
	detach_virtual_machine_ip_address,
	get_virtual_machine,
	list_virtual_machines,
	restart_virtual_machine,
	start_virtual_machine,
	stop_virtual_machine,
	update_virtual_machine_compute,
)
from atlas.api.tests.test_support import OTHER_TENANT_ID, TENANT_ID, api_request, call_route

CREATE_BODY = {
	"image_id": "system-image",
	"vcpus": 2,
	"memory_mib": 2048,
	"disk_mib": 20480,
}


def build_virtual_machine(tenant_id: int = TENANT_ID, **overrides) -> SimpleNamespace:
	"""Return one stored virtual machine row."""
	values = {
		"name": "vm-00001",
		"tenant_id": tenant_id,
		"virtual_machine_image": "system-image",
		"vcpus": 2,
		"memory_mib": 2048,
		"disk_mib": 20480,
		"sleep_after_idle_seconds": 0,
		"is_privileged": 0,
		"is_draft": 0,
		"is_terminating": 0,
		"creation": "2026-09-08T10:00:00+05:30",
		"set_power_state": Mock(),
		"reboot": Mock(),
		"terminate": Mock(),
		"get_metal_vm_info": Mock(return_value=None),
		"update_compute": Mock(),
		"attach_ip_address": Mock(),
		"detach_ip_address": Mock(),
	}
	values.update(overrides)
	return SimpleNamespace(**values)


def build_metal_information() -> SimpleNamespace:
	"""Return one Metal record with a desired and an observed half."""
	return SimpleNamespace(
		desired=SimpleNamespace(
			state="running",
			disk=SimpleNamespace(throughput_mibps=100, iops=500),
			network=SimpleNamespace(
				egress="uplink",
				public_ipv4="203.0.113.10",
				wireguard_mesh_ipv6="fdaa:1::5",
				private_network_throughput_mibps=0,
				public_network_throughput_mibps=0,
			),
			guest=SimpleNamespace(
				hostname="worker-1",
				ssh_keys=("ssh-ed25519 AAAA",),
				metadata={"role": "worker"},
			),
		),
		observed=SimpleNamespace(
			state="stopped",
			disk=SimpleNamespace(used_mib=8123),
			network=SimpleNamespace(mac="52:54:00:12:34:56"),
			error=SimpleNamespace(message="boot failed"),
		),
	)


def stored_rows(rows: list[SimpleNamespace]):
	"""Patch the Virtual Machine query and leave every other query alone."""
	query = frappe.get_list

	def get_list(doctype, *args, **kwargs):
		return rows if doctype == "Virtual Machine" else query(doctype, *args, **kwargs)

	return patch("atlas.api.routes.virtual_machines.frappe.get_list", side_effect=get_list)


def owned_document(virtual_machine: SimpleNamespace):
	"""Patch the ownership lookup so it returns one virtual machine."""
	return patch("atlas.api.routes.virtual_machines.get_owned_document", return_value=virtual_machine)


class TestVirtualMachineViews(UnitTestCase):
	def test_summary_hides_the_assigned_host(self) -> None:
		summary = VirtualMachineResponse.from_document(build_virtual_machine())

		self.assertEqual(summary.id, "vm-00001")
		self.assertEqual(summary.tenant_id, TENANT_ID)
		self.assertEqual(summary.image_id, "system-image")
		self.assertIsInstance(summary.created_at, int)
		self.assertNotIn("virtual_machine_image_id", summary.model_fields)
		self.assertNotIn("server", summary.model_fields)

	def test_detail_carries_the_addresses_and_the_guest_configuration(self) -> None:
		detail = VirtualMachineDetailResponse.from_document_and_metal(
			build_virtual_machine(), build_metal_information()
		)

		self.assertEqual(detail.desired_state, "running")
		self.assertEqual(detail.current_state, "stopped")
		self.assertEqual(detail.network.public_ipv4, "203.0.113.10")
		self.assertEqual(detail.network.mesh_ipv6, "fdaa:1::5")
		self.assertEqual(detail.network.mac, "52:54:00:12:34:56")
		self.assertEqual(detail.network.egress, "uplink")
		self.assertEqual(detail.disk.iops, 500)
		self.assertEqual(detail.disk.used_mib, 8123)
		self.assertEqual(detail.guest.ssh_keys, ["ssh-ed25519 AAAA"])
		self.assertEqual(detail.guest.metadata, {"role": "worker"})
		self.assertEqual(detail.error, "boot failed")

	def test_detail_hides_the_host_operation_data(self) -> None:
		detail = VirtualMachineDetailResponse.from_document_and_metal(
			build_virtual_machine(), build_metal_information()
		)

		for field in ("operation_id", "phase", "generation", "restart_generation", "server"):
			self.assertNotIn(field, detail.model_fields)

	def test_detail_without_metal_state_reports_unknown(self) -> None:
		detail = VirtualMachineDetailResponse.from_document_and_metal(build_virtual_machine(), None)

		self.assertIsNone(detail.desired_state)
		self.assertEqual(detail.current_state, "unknown")
		self.assertIsNone(detail.network.public_ipv4)
		self.assertEqual(detail.guest.ssh_keys, [])


class TestCreateVirtualMachine(UnitTestCase):
	def create(self, body: dict, tenant_id: int | str | None = TENANT_ID):
		"""Run the create route against one request body."""
		virtual_machine = build_virtual_machine(is_draft=1)
		with (
			api_request("POST", "/api/atlas/virtual-machines", tenant_id=tenant_id, json=body),
			patch(
				"atlas.api.routes.virtual_machines.get_owned_image",
				return_value=SimpleNamespace(name="system-image"),
			),
			patch(
				"atlas.api.routes.virtual_machines.create_virtual_machine_request",
				return_value={"name": "vm-00001", "is_draft": True},
			) as create,
			patch("atlas.api.routes.virtual_machines.frappe.get_doc", return_value=virtual_machine),
		):
			return (*call_route(create_virtual_machine), create)

	def test_create_returns_the_record_and_its_location(self) -> None:
		status, body, create = self.create(CREATE_BODY)

		self.assertEqual(status, 201)
		self.assertEqual(body["id"], "vm-00001")
		self.assertEqual(create.call_args.args[0].tenant_id, TENANT_ID)

	def test_create_needs_a_tenant_header(self) -> None:
		status, body, _ = self.create(CREATE_BODY, tenant_id=None)

		self.assertEqual(status, 400)
		self.assertEqual(body["error"]["code"], "invalid_request")

	def test_create_passes_the_privileged_flag(self) -> None:
		status, _, create = self.create({**CREATE_BODY, "is_privileged": True}, tenant_id=0)

		self.assertEqual(status, 201)
		self.assertTrue(create.call_args.args[0].is_privileged)

	def test_create_rejects_a_field_the_caller_cannot_set(self) -> None:
		for field in ("tenant_id", "server", "is_sleepy", "idle_timeout_seconds"):
			status, body, _ = self.create({**CREATE_BODY, field: 1})

			self.assertEqual(status, 400)
			self.assertIn(field, [item["name"] for item in body["error"]["fields"]])

	def test_create_rejects_values_that_are_not_positive(self) -> None:
		status, _, _ = self.create({**CREATE_BODY, "vcpus": 0})

		self.assertEqual(status, 400)

	def test_create_rejects_an_invalid_idle_timeout(self) -> None:
		for value in (-1, 9_223_372_037):
			status, body, _ = self.create({**CREATE_BODY, "sleep_after_idle_seconds": value})

			self.assertEqual(status, 400)
			self.assertEqual([item["name"] for item in body["error"]["fields"]], ["sleep_after_idle_seconds"])


class TestReadVirtualMachines(UnitTestCase):
	def test_list_filters_by_tenant_and_pages(self) -> None:
		rows = [build_virtual_machine(name=f"vm-{index}") for index in range(3)]
		with (
			api_request(
				"GET", "/api/atlas/virtual-machines", tenant_id=TENANT_ID, query_string={"limit": "2"}
			),
			stored_rows(rows) as get_list,
			patch("atlas.api.routes.virtual_machines.get_reported_state_rows", return_value={}),
		):
			status, body = call_route(list_virtual_machines)

		self.assertEqual(status, 200)
		self.assertEqual(get_list.call_args.kwargs["filters"], {"tenant_id": TENANT_ID})
		self.assertEqual(get_list.call_args.kwargs["limit"], 3)
		self.assertEqual(len(body["items"]), 2)
		self.assertTrue(body["has_more"])

	def test_list_adds_the_last_reported_state(self) -> None:
		"""The list route reads stored state and does not contact the host."""
		rows = [build_virtual_machine(), build_virtual_machine(name="vm-00002")]
		state = SimpleNamespace(status="running", synced_at="2026-09-08 10:05:00")
		with (
			api_request("GET", "/api/atlas/virtual-machines", tenant_id=TENANT_ID),
			stored_rows(rows),
			patch(
				"atlas.api.routes.virtual_machines.get_reported_state_rows",
				return_value={"vm-00001": state},
			),
		):
			status, body = call_route(list_virtual_machines)

		self.assertEqual(status, 200)
		self.assertEqual(body["items"][0]["last_known_state"], "running")
		self.assertIsNotNone(body["items"][0]["state_synced_at"])
		self.assertEqual(body["items"][1]["last_known_state"], "unknown")
		self.assertIsNone(body["items"][1]["state_synced_at"])
		rows[0].get_metal_vm_info.assert_not_called()

	def test_list_shows_a_draft_as_pending(self) -> None:
		rows = [build_virtual_machine(is_draft=1)]
		with (
			api_request("GET", "/api/atlas/virtual-machines", tenant_id=TENANT_ID),
			stored_rows(rows),
			patch("atlas.api.routes.virtual_machines.get_reported_state_rows", return_value={}),
		):
			status, body = call_route(list_virtual_machines)

		self.assertEqual(status, 200)
		self.assertEqual(body["items"][0]["last_known_state"], "pending")

	def test_read_adds_the_live_host_state(self) -> None:
		virtual_machine = build_virtual_machine()
		with (
			api_request("GET", "/api/atlas/virtual-machines/vm-00001", tenant_id=TENANT_ID),
			owned_document(virtual_machine),
		):
			status, body = call_route(get_virtual_machine, virtual_machine_id="vm-00001")

		self.assertEqual(status, 200)
		self.assertEqual(body["id"], "vm-00001")
		self.assertIsNone(body["desired_state"])
		self.assertEqual(body["current_state"], "unknown")


class TestVirtualMachineActions(UnitTestCase):
	def run_action(self, handler, **kwargs):
		"""Run one action route against a managed virtual machine."""
		virtual_machine = build_virtual_machine()
		with (
			api_request("POST", "/api/atlas/virtual-machines/vm-00001/actions", tenant_id=TENANT_ID),
			owned_document(virtual_machine),
		):
			status, body = call_route(handler, virtual_machine_id="vm-00001", **kwargs)

		return status, body, virtual_machine

	def test_start_and_stop_request_a_power_state(self) -> None:
		status, _, service = self.run_action(start_virtual_machine)
		self.assertEqual(status, 202)
		service.set_power_state.assert_called_once_with("running")

		_, _, service = self.run_action(stop_virtual_machine)
		service.set_power_state.assert_called_once_with("stopped")

	def test_restart_asks_the_host_for_one_restart(self) -> None:
		status, body, service = self.run_action(restart_virtual_machine)

		self.assertEqual(status, 202)
		self.assertEqual(body["id"], "vm-00001")
		service.reboot.assert_called_once_with()

	def test_delete_requests_termination(self) -> None:
		virtual_machine = build_virtual_machine()

		virtual_machine.terminate.side_effect = lambda: setattr(virtual_machine, "is_terminating", 1)

		with (
			api_request("DELETE", "/api/atlas/virtual-machines/vm-00001", tenant_id=TENANT_ID),
			owned_document(virtual_machine),
		):
			status, body = call_route(delete_virtual_machine, virtual_machine_id="vm-00001")

		self.assertEqual(status, 202)
		self.assertEqual(body["id"], "vm-00001")
		virtual_machine.terminate.assert_called_once()


class TestVirtualMachineConfiguration(UnitTestCase):
	def test_compute_change_calls_the_virtual_machine_method(self) -> None:
		with (
			api_request(
				"PATCH",
				"/api/atlas/virtual-machines/vm-00001/compute",
				tenant_id=TENANT_ID,
				json={"vcpus": 4},
			),
			owned_document(virtual_machine := build_virtual_machine()),
		):
			status, body = call_route(update_virtual_machine_compute, virtual_machine_id="vm-00001")

		self.assertEqual(status, 202)
		self.assertEqual(body["id"], "vm-00001")
		virtual_machine.update_compute.assert_called_once_with({"virtual_cpu_count": 4})

	def test_compute_change_keeps_the_value_that_is_absent(self) -> None:
		with (
			api_request(
				"PATCH",
				"/api/atlas/virtual-machines/vm-00001/compute",
				tenant_id=TENANT_ID,
				json={"vcpus": 4},
			),
			owned_document(virtual_machine := build_virtual_machine()),
		):
			status, _ = call_route(update_virtual_machine_compute, virtual_machine_id="vm-00001")

		self.assertEqual(status, 202)
		virtual_machine.update_compute.assert_called_once_with({"virtual_cpu_count": 4})

	def test_an_idle_timeout_change_reaches_the_virtual_machine_method(self) -> None:
		with (
			api_request(
				"PATCH",
				"/api/atlas/virtual-machines/vm-00001/compute",
				tenant_id=TENANT_ID,
				json={"sleep_after_idle_seconds": 1800},
			),
			owned_document(virtual_machine := build_virtual_machine()),
		):
			status, _ = call_route(update_virtual_machine_compute, virtual_machine_id="vm-00001")

		self.assertEqual(status, 202)
		virtual_machine.update_compute.assert_called_once_with({"sleep_after_idle_seconds": 1800})

	def test_a_patch_without_a_supported_field_is_rejected(self) -> None:
		with (
			api_request(
				"PATCH",
				"/api/atlas/virtual-machines/vm-00001/compute",
				tenant_id=TENANT_ID,
				json={},
			),
			owned_document(build_virtual_machine()),
		):
			status, body = call_route(update_virtual_machine_compute, virtual_machine_id="vm-00001")

		self.assertEqual(status, 400)
		self.assertEqual(body["error"]["code"], "invalid_request")

	def test_compute_change_rejects_old_idle_fields(self) -> None:
		for field in ("is_sleepy", "idle_timeout_seconds"):
			with (
				api_request(
					"PATCH",
					"/api/atlas/virtual-machines/vm-00001/compute",
					tenant_id=TENANT_ID,
					json={field: 1},
				),
				owned_document(build_virtual_machine()),
			):
				status, body = call_route(update_virtual_machine_compute, virtual_machine_id="vm-00001")

			self.assertEqual(status, 400)
			self.assertIn(field, [item["name"] for item in body["error"]["fields"]])

	def attach(self, attached: str | None, requested: str):
		"""Run the attach route with one currently attached address."""
		with (
			api_request(
				"PUT",
				"/api/atlas/virtual-machines/vm-00001/ip-address",
				tenant_id=TENANT_ID,
				json={"ip_address_id": requested},
			),
			owned_document(virtual_machine := build_virtual_machine()),
			patch("atlas.api.routes.virtual_machines.frappe.db.get_value", return_value=attached),
			patch(
				"atlas.api.routes.virtual_machines.get_available_ip_address",
				return_value=SimpleNamespace(name=requested),
			),
		):
			status, body = call_route(attach_virtual_machine_ip_address, virtual_machine_id="vm-00001")

		return status, body, virtual_machine

	def test_attaching_the_same_address_again_is_safe(self) -> None:
		status, _, service = self.attach("203.0.113.10", "203.0.113.10")

		self.assertEqual(status, 202)
		service.attach_ip_address.assert_not_called()

	def test_attaching_a_different_address_is_rejected(self) -> None:
		status, body, service = self.attach("203.0.113.10", "203.0.113.11")

		self.assertEqual(status, 409)
		self.assertEqual(body["error"]["code"], "conflict")
		service.attach_ip_address.assert_not_called()

	def test_attaching_the_first_address_reaches_the_service(self) -> None:
		status, _, service = self.attach(None, "203.0.113.11")

		self.assertEqual(status, 202)
		service.attach_ip_address.assert_called_once_with("203.0.113.11")

	def test_detaching_without_an_address_changes_nothing(self) -> None:
		with (
			api_request("DELETE", "/api/atlas/virtual-machines/vm-00001/ip-address", tenant_id=TENANT_ID),
			owned_document(virtual_machine := build_virtual_machine()),
			patch("atlas.api.routes.virtual_machines.frappe.db.exists", return_value=False),
		):
			status, _ = call_route(detach_virtual_machine_ip_address, virtual_machine_id="vm-00001")

		self.assertEqual(status, 202)
		virtual_machine.detach_ip_address.assert_not_called()
