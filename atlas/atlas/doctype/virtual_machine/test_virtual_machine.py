from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.tests._mocks import fake_task
from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine


def _ensure_test_server() -> str:
	provider = make_provider("vm-test-provider")
	server = make_server(
		provider,
		"vm-test-server",
		ipv4_address="10.0.0.99",
		ipv6_address="2001:db8:1::1",
		ipv6_prefix="2001:db8:1::/64",
		ipv6_virtual_machine_range="2001:db8:1::/124",
		status="Active",
	)
	return server.name


def _ensure_test_image() -> str:
	return make_image("vm-test-image-2").name


def _new_vm(**overrides) -> "frappe.model.document.Document":
	return make_virtual_machine(_ensure_test_server(), _ensure_test_image(), **overrides)


class TestVirtualMachine(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		# Clear VMs from prior tests so the /124 IPv6 range has capacity.
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def test_before_insert_sets_uuid_mac_tap_ipv6(self) -> None:
		vm = _new_vm()
		# Frappe-validated UUID: 36 chars with 4 dashes
		self.assertEqual(len(vm.name), 36)
		self.assertEqual(vm.name.count("-"), 4)
		self.assertTrue(vm.mac_address.startswith("06:00:"))
		self.assertTrue(vm.tap_device.startswith("atlas-"))
		self.assertEqual(len(vm.tap_device), 15)
		self.assertTrue(vm.ipv6_address.startswith("2001:db8:1::"))
		self.assertEqual(vm.status, "Pending")

	def test_immutable_fields_raise(self) -> None:
		vm = _new_vm()
		vm.vcpus = 4
		with self.assertRaises(frappe.ValidationError):
			vm.save(ignore_permissions=True)

	def test_provision_runs_when_image_present(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		task = fake_task(name="task-prov-1")

		with patch.object(module, "run_task", return_value=task) as mocked:
			vm.provision()
		vm.reload()
		self.assertEqual(vm.status, "Running")
		self.assertIsNotNone(vm.last_started)
		# One Task per VM creation: provision-vm.sh's step 0 is the image probe.
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "provision-vm.sh")

	def test_provision_failure_leaves_status_pending(self) -> None:
		"""On failure the row is not mutated (Pilot shape). Task row carries
		the failure; operator re-clicks Provision (scripts are idempotent)."""
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		with patch.object(
			module,
			"run_task",
			side_effect=frappe.ValidationError("provision broke"),
		):
			with self.assertRaises(frappe.ValidationError):
				vm.provision()
		vm.reload()
		self.assertEqual(vm.status, "Pending")

	def test_provision_rejects_from_running(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		vm.db_set("status", "Running")
		vm.reload()
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				vm.provision()
		self.assertIn("Cannot provision from Running", str(raised.exception))
		mocked.assert_not_called()

	def test_validate_skips_when_no_before_save(self) -> None:
		# Defensive branch: a non-new VM whose `_doc_before_save` was cleared
		# should early-return from validate without comparing immutables.
		vm = _new_vm()
		vm._doc_before_save = None
		vm.vcpus = 99
		# Directly invoke validate; should not throw.
		vm.validate()

	def test_set_status_default_assigns_pending_when_empty(self) -> None:
		# Frappe's JSON default pre-populates status, so we have to clear it
		# in-memory to exercise the assignment branch.
		vm = frappe.get_doc({
			"doctype": "Virtual Machine",
			"server": _ensure_test_server(),
			"image": _ensure_test_image(),
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 2,
			"ssh_public_key": "ssh-ed25519 AAAA",
		})
		vm.status = None
		vm.set_status_default()
		self.assertEqual(vm.status, "Pending")

	def test_set_status_default_keeps_existing(self) -> None:
		# `set_status_default` is a no-op when status is already populated.
		# Construct an in-memory VM and exercise the helper directly.
		vm = frappe.get_doc({
			"doctype": "Virtual Machine",
			"server": _ensure_test_server(),
			"image": _ensure_test_image(),
			"status": "Stopped",
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 2,
			"ssh_public_key": "ssh-ed25519 AAAA",
		})
		vm.set_status_default()
		self.assertEqual(vm.status, "Stopped")

	def test_set_ipv6_address_keeps_existing(self) -> None:
		vm = make_virtual_machine(
			_ensure_test_server(),
			_ensure_test_image(),
			ipv6_address="2001:db8:1::abcd",
		)
		self.assertEqual(vm.ipv6_address, "2001:db8:1::abcd")

	def test_set_mac_address_keeps_existing(self) -> None:
		vm = make_virtual_machine(
			_ensure_test_server(),
			_ensure_test_image(),
			mac_address="06:00:11:22:33:44",
		)
		self.assertEqual(vm.mac_address, "06:00:11:22:33:44")

	def test_set_tap_device_keeps_existing(self) -> None:
		vm = make_virtual_machine(
			_ensure_test_server(),
			_ensure_test_image(),
			tap_device="atlas-aabbccdd1",
		)
		self.assertEqual(vm.tap_device, "atlas-aabbccdd1")
