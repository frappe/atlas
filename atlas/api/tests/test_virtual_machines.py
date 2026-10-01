from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
import orjson
from frappe.tests import UnitTestCase

from atlas.api.models import SnapshotPayload, VirtualMachineDetailResponse, VirtualMachineResponse
from atlas.api.routes.virtual_machines import (
	attach_virtual_machine_public_ipv4,
	attach_virtual_machine_public_ipv6,
	create_virtual_machine,
	delete_virtual_machine,
	detach_virtual_machine_public_ipv4,
	detach_virtual_machine_public_ipv6,
	get_virtual_machine,
	list_virtual_machines,
	resize_virtual_machine,
	restart_virtual_machine,
	start_virtual_machine,
	stop_virtual_machine,
	update_virtual_machine_network,
)
from atlas.api.tests.test_support import (
	OTHER_TENANT_ID,
	TENANT_ID,
	api_request,
	call_route,
	route_response,
)
from atlas.atlas.core.tags import (
	MAXIMUM_TAG_KEY_LENGTH,
	MAXIMUM_TAG_VALUE_LENGTH,
	MAXIMUM_TAGS,
)
from atlas.vm.core.metal_models import MetalFirewall, MetalFirewallRule
from atlas.vm.core.models import DEFAULT_ROUTES, Route
from atlas.vm.core.placement import OutOfCapacity

CREATE_BODY = {
	"image_id": "system-image",
	"cpu_millicores": 2000,
	"memory_mib": 2048,
	"disk_mib": 20480,
}


def build_virtual_machine(tenant_id: int = TENANT_ID, **overrides) -> SimpleNamespace:
	"""Return one stored virtual machine row."""
	values = {
		"name": "vm-00001",
		"tenant_id": tenant_id,
		"virtual_machine_image": "system-image",
		"architecture": "amd64",
		"cpu_millicores": 2000,
		"memory_mib": 2048,
		"disk_mib": 20480,
		"sleep_after_idle_seconds": 0,
		"is_privileged": 0,
		"is_termination_protected": 0,
		"is_draft": 0,
		"is_terminating": 0,
		"active_migration": None,
		"creation": "2026-09-08T10:00:00+05:30",
		"set_power_state": Mock(),
		"reboot": Mock(),
		"terminate": Mock(),
		"get_metal_vm_info": Mock(return_value=None),
		"public_ipv6": "2001:db8:5::7/128",
		"resize": Mock(),
		"attach_public_ip": Mock(),
		"detach_public_ip": Mock(),
		"update_network": Mock(),
		"set_termination_protection": Mock(),
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
				routes=(Route("0.0.0.0/0", "host"),),
				public_ipv4="203.0.113.10",
				wireguard_mesh_ipv6="fdaa:1::5",
				private_network_throughput_mibps=0,
				public_network_throughput_mibps=0,
				firewall=MetalFirewall(
					enabled=True,
					inbound=(MetalFirewallRule("tcp", "22", ("203.0.113.0/24",)),),
					outbound=(),
				),
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


def stored_tags(tags: dict[str, dict[str, str]] | None = None):
	"""Patch the tag query that a list route runs for its page of rows."""
	return patch(
		"atlas.api.routes.virtual_machines.read_tags_for",
		side_effect=lambda doctype, names: {name: (tags or {}).get(name, {}) for name in names},
	)


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
		self.assertTrue(detail.network.ipv4_internet_access)
		self.assertEqual(detail.network.public_ipv6, "2001:db8:5::7/128")
		self.assertTrue(detail.network.firewall.enabled)
		self.assertEqual(detail.network.firewall.inbound[0].ports, "22")
		self.assertEqual(detail.disk.iops, 500)
		self.assertEqual(detail.disk.used_mib, 8123)
		self.assertEqual(detail.guest.ssh_keys, ["ssh-ed25519 AAAA"])
		self.assertEqual(detail.guest.metadata, {"role": "worker"})
		self.assertEqual(detail.error, "boot failed")

	def test_detail_requires_the_default_route_for_ipv4_internet_access(self) -> None:
		information = build_metal_information()
		information.desired.network.routes = (Route("10.0.0.0/8", "host"),)

		detail = VirtualMachineDetailResponse.from_document_and_metal(build_virtual_machine(), information)

		self.assertFalse(detail.network.ipv4_internet_access)

	def test_a_migration_hides_the_host_state(self) -> None:
		detail = VirtualMachineDetailResponse.from_document_and_metal(
			build_virtual_machine(active_migration="mig-00001"), build_metal_information()
		)

		self.assertEqual(detail.current_state, "migrating")

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

		self.assertEqual(status, 202)
		self.assertEqual(body["id"], "vm-00001")
		self.assertEqual(create.call_args.args[0].tenant_id, TENANT_ID)

	def test_create_reports_out_of_capacity_without_retry_header(self) -> None:
		with (
			api_request("POST", "/api/atlas/virtual-machines", tenant_id=TENANT_ID, json=CREATE_BODY),
			patch(
				"atlas.api.routes.virtual_machines.get_owned_image",
				return_value=SimpleNamespace(name="system-image"),
			),
			patch(
				"atlas.api.routes.virtual_machines.create_virtual_machine_request",
				side_effect=OutOfCapacity("Retry later"),
			),
		):
			response = route_response(create_virtual_machine)

		self.assertEqual(response.status_code, 503)
		self.assertEqual(orjson.loads(response.get_data())["error"]["code"], "out_of_capacity")
		self.assertNotIn("Retry-After", response.headers)

	def test_create_needs_a_tenant_header(self) -> None:
		status, body, _ = self.create(CREATE_BODY, tenant_id=None)

		self.assertEqual(status, 400)
		self.assertEqual(body["error"]["code"], "invalid_request")

	def test_create_passes_the_privileged_flag(self) -> None:
		status, _, create = self.create({**CREATE_BODY, "is_privileged": True}, tenant_id=0)

		self.assertEqual(status, 202)
		self.assertTrue(create.call_args.args[0].is_privileged)

	def test_create_passes_the_firewall(self) -> None:
		status, _, create = self.create(
			{
				**CREATE_BODY,
				"firewall": {
					"enabled": True,
					"inbound": [{"protocol": "tcp", "ports": "22", "cidrs": ["203.0.113.0/24"]}],
				},
			}
		)

		self.assertEqual(status, 202)
		firewall = create.call_args.args[0].firewall
		self.assertTrue(firewall.enabled)
		self.assertEqual(firewall.inbound[0].cidrs, ("203.0.113.0/24",))

	def test_create_passes_both_public_ip_selectors(self) -> None:
		status, _, create = self.create({**CREATE_BODY, "public_ipv4": "auto", "public_ipv6": "auto"})
		self.assertEqual(status, 202)
		self.assertEqual(create.call_args.args[0].public_ipv4, "auto")
		self.assertEqual(create.call_args.args[0].public_ipv6, "auto")

	def test_create_turns_off_ipv4_internet_access(self) -> None:
		status, _, create = self.create({**CREATE_BODY, "ipv4_internet_access": False})

		self.assertEqual(status, 202)
		self.assertEqual(create.call_args.args[0].routes, ())

	def test_create_uses_ipv4_internet_access_by_default(self) -> None:
		status, _, create = self.create(CREATE_BODY)

		self.assertEqual(status, 202)
		self.assertEqual(create.call_args.args[0].routes, DEFAULT_ROUTES)

	def test_create_refuses_routes_and_a_public_ipv4_without_ipv4_internet_access(self) -> None:
		for body in (
			{**CREATE_BODY, "routes": [{"destination": "0.0.0.0/0", "via": "host"}]},
			{**CREATE_BODY, "public_ipv4": "auto", "ipv4_internet_access": False},
		):
			status, _, create = self.create(body)
			self.assertEqual(status, 400)
			create.assert_not_called()

	def test_create_rejects_a_field_the_caller_cannot_set(self) -> None:
		for field in ("tenant_id", "server", "is_sleepy", "idle_timeout_seconds"):
			status, body, _ = self.create({**CREATE_BODY, field: 1})

			self.assertEqual(status, 400)
			self.assertIn(field, [item["name"] for item in body["error"]["fields"]])

	def test_create_rejects_cpu_below_the_minimum(self) -> None:
		status, _, _ = self.create({**CREATE_BODY, "cpu_millicores": 99})

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
			stored_tags(),
		):
			status, body = call_route(list_virtual_machines)

		self.assertEqual(status, 200)
		virtual_machine_call = next(
			call for call in get_list.call_args_list if call.args[0] == "Virtual Machine"
		)
		self.assertEqual(virtual_machine_call.kwargs["filters"], {"tenant_id": TENANT_ID})
		self.assertEqual(virtual_machine_call.kwargs["limit"], 3)
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
			stored_tags(),
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
			stored_tags(),
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
	def test_network_change_passes_a_partial_firewall(self) -> None:
		with (
			api_request(
				"PATCH",
				"/api/atlas/virtual-machines/vm-00001/network",
				tenant_id=TENANT_ID,
				json={"firewall": {"enabled": True}},
			),
			owned_document(virtual_machine := build_virtual_machine()),
		):
			status, _ = call_route(update_virtual_machine_network, virtual_machine_id="vm-00001")

		self.assertEqual(status, 202)
		virtual_machine.update_network.assert_called_once_with({"firewall": {"enabled": True}})

	def test_network_change_passes_ipv4_internet_access(self) -> None:
		with (
			api_request(
				"PATCH",
				"/api/atlas/virtual-machines/vm-00001/network",
				tenant_id=TENANT_ID,
				json={"ipv4_internet_access": False},
			),
			owned_document(virtual_machine := build_virtual_machine(update_network=Mock(return_value={}))),
		):
			status, _ = call_route(update_virtual_machine_network, virtual_machine_id="vm-00001")

		self.assertEqual(status, 202)
		virtual_machine.update_network.assert_called_once_with({"ipv4_internet_access": False})

	def test_network_change_rejects_null_ipv4_internet_access(self) -> None:
		with (
			api_request(
				"PATCH",
				"/api/atlas/virtual-machines/vm-00001/network",
				tenant_id=TENANT_ID,
				json={"ipv4_internet_access": None},
			),
			owned_document(virtual_machine := build_virtual_machine()),
		):
			status, _ = call_route(update_virtual_machine_network, virtual_machine_id="vm-00001")

		self.assertEqual(status, 400)
		virtual_machine.update_network.assert_not_called()

	def test_network_change_ignores_nullable_rate_limits(self) -> None:
		with (
			api_request(
				"PATCH",
				"/api/atlas/virtual-machines/vm-00001/network",
				tenant_id=TENANT_ID,
				json={"private_network_throughput_mibps": None, "firewall": None},
			),
			owned_document(virtual_machine := build_virtual_machine()),
		):
			status, _ = call_route(update_virtual_machine_network, virtual_machine_id="vm-00001")

		self.assertEqual(status, 202)
		virtual_machine.update_network.assert_called_once_with({})

	def test_network_change_rejects_an_invalid_firewall_rule(self) -> None:
		with (
			api_request(
				"PATCH",
				"/api/atlas/virtual-machines/vm-00001/network",
				tenant_id=TENANT_ID,
				json={"firewall": {"inbound": [{"protocol": "tcp", "ports": "0", "cidrs": ["0.0.0.0/0"]}]}},
			),
			owned_document(virtual_machine := build_virtual_machine()),
		):
			status, _ = call_route(update_virtual_machine_network, virtual_machine_id="vm-00001")

		self.assertEqual(status, 400)
		virtual_machine.update_network.assert_not_called()

	def resize(self, json: dict) -> tuple[int, dict, SimpleNamespace]:
		"""Run the resize route with one request body."""
		with (
			api_request(
				"POST", "/api/atlas/virtual-machines/vm-00001/actions/resize", tenant_id=TENANT_ID, json=json
			),
			owned_document(virtual_machine := build_virtual_machine()),
		):
			status, body = call_route(resize_virtual_machine, virtual_machine_id="vm-00001")
		return status, body, virtual_machine

	def test_resize_passes_only_the_given_values(self) -> None:
		status, body, virtual_machine = self.resize({"cpu_millicores": 4000, "disk_mib": 40960})

		self.assertEqual(status, 202)
		self.assertEqual(body["id"], "vm-00001")
		virtual_machine.resize.assert_called_once_with(cpu_millicores=4000, disk_mib=40960)

	def test_resize_accepts_an_idle_shutdown_change_alone(self) -> None:
		status, _body, virtual_machine = self.resize({"sleep_after_idle_seconds": 1800})

		self.assertEqual(status, 202)
		virtual_machine.resize.assert_called_once_with(sleep_after_idle_seconds=1800)

	def test_resize_rejects_an_invalid_body(self) -> None:
		for json in ({}, {"cpu_millicores": 99}, {"disk_mib": 0}, {"is_sleepy": 1}):
			status, body, virtual_machine = self.resize(json)

			self.assertEqual(status, 400, json)
			self.assertEqual(body["error"]["code"], "invalid_request")
			virtual_machine.resize.assert_not_called()

	def attach(self, version: int, requested: str):
		"""Run one public IP attach route."""
		route = attach_virtual_machine_public_ipv4 if version == 4 else attach_virtual_machine_public_ipv6
		with (
			api_request(
				"PUT",
				f"/api/atlas/virtual-machines/vm-00001/public-ipv{version}",
				tenant_id=TENANT_ID,
				json={"public_ip": requested},
			),
			owned_document(virtual_machine := build_virtual_machine()),
		):
			status, body = call_route(route, virtual_machine_id="vm-00001")

		return status, body, virtual_machine

	def test_auto_ipv4_reaches_the_domain_service(self) -> None:
		status, _, virtual_machine = self.attach(4, "auto")
		self.assertEqual(status, 202)
		virtual_machine.attach_public_ip.assert_called_once_with(4, "auto")

	def test_reserved_ipv6_reaches_the_domain_service(self) -> None:
		allocation = "32eb57bc-9548-4a89-8358-543e26883569"
		status, _, virtual_machine = self.attach(6, allocation)
		self.assertEqual(status, 202)
		virtual_machine.attach_public_ip.assert_called_once_with(6, allocation)

	def test_detach_routes_are_idempotent_at_the_domain_boundary(self) -> None:
		for version, route in (
			(4, detach_virtual_machine_public_ipv4),
			(6, detach_virtual_machine_public_ipv6),
		):
			virtual_machine = build_virtual_machine()
			with (
				api_request(
					"DELETE",
					f"/api/atlas/virtual-machines/vm-00001/public-ipv{version}",
					tenant_id=TENANT_ID,
				),
				owned_document(virtual_machine),
			):
				status, _ = call_route(route, virtual_machine_id="vm-00001")
			self.assertEqual(status, 202)
			virtual_machine.detach_public_ip.assert_called_once_with(version)

	def test_an_unknown_attach_field_is_rejected(self) -> None:
		with (
			api_request(
				"PUT",
				"/api/atlas/virtual-machines/vm-00001/public-ipv4",
				tenant_id=TENANT_ID,
				json={"allocation": "auto"},
			),
			owned_document(virtual_machine := build_virtual_machine()),
		):
			status, _ = call_route(attach_virtual_machine_public_ipv4, virtual_machine_id="vm-00001")
		self.assertEqual(status, 400)
		virtual_machine.attach_public_ip.assert_not_called()


class TestSnapshotPayload(UnitTestCase):
	def test_a_tag_map_within_every_limit_is_accepted(self) -> None:
		tags = {"k" * MAXIMUM_TAG_KEY_LENGTH: "v" * MAXIMUM_TAG_VALUE_LENGTH}

		payload = SnapshotPayload(title="image", tags=tags)

		self.assertEqual(payload.tags, tags)

	def test_a_long_tag_key_is_rejected(self) -> None:
		with self.assertRaises(ValueError):
			SnapshotPayload(title="image", tags={"k" * (MAXIMUM_TAG_KEY_LENGTH + 1): "a"})

	def test_a_long_tag_value_is_rejected(self) -> None:
		with self.assertRaises(ValueError):
			SnapshotPayload(title="image", tags={"os": "v" * (MAXIMUM_TAG_VALUE_LENGTH + 1)})

	def test_too_many_tags_are_rejected(self) -> None:
		tags = {f"key-{index}": "a" for index in range(MAXIMUM_TAGS + 1)}

		with self.assertRaises(ValueError):
			SnapshotPayload(title="image", tags=tags)
