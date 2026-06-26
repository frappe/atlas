from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.port_mapping.port_mapping import (
	allocate_port,
	port_map,
	port_pool,
)
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)


def _purge() -> None:
	for name in frappe.get_all("Port Mapping", pluck="name"):
		frappe.delete_doc("Port Mapping", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


def _make_mapping(vm: str, target_port: int = 22, **overrides):
	doc = {
		"doctype": "Port Mapping",
		"virtual_machine": vm,
		"target_port": target_port,
	}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


class TestPortMapping(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()
		# Pin a small, deterministic pool so the allocation tests are cheap and the
		# exhaustion test is reachable. Restored implicitly by the next test's setUp.
		frappe.db.set_single_value("Atlas Settings", "tcp_port_pool", "10000-10002")

	# --- allocation --------------------------------------------------------

	def test_first_mapping_gets_pool_low(self) -> None:
		vm = _new_vm()
		mapping = _make_mapping(vm.name)
		self.assertEqual(mapping.public_port, 10000)

	def test_ports_allocate_lowest_free_ascending(self) -> None:
		a, b, c = _new_vm(), _new_vm(), _new_vm()
		self.assertEqual(_make_mapping(a.name).public_port, 10000)
		self.assertEqual(_make_mapping(b.name).public_port, 10001)
		self.assertEqual(_make_mapping(c.name).public_port, 10002)

	def test_freed_low_port_is_reused(self) -> None:
		a, b = _new_vm(), _new_vm()
		first = _make_mapping(a.name)
		self.assertEqual(first.public_port, 10000)
		_make_mapping(b.name)  # 10001
		frappe.delete_doc("Port Mapping", first.name, force=1, ignore_permissions=True)
		# 10000 is free again → the next allocation reuses the lowest hole.
		c = _new_vm()
		self.assertEqual(_make_mapping(c.name).public_port, 10000)

	def test_inactive_mapping_still_owns_its_port(self) -> None:
		# An inactive row is excluded from the served map but STILL holds its port —
		# toggling it back on must never collide with a port handed out meanwhile.
		a, b = _new_vm(), _new_vm()
		inactive = _make_mapping(a.name, active=0)
		self.assertEqual(inactive.public_port, 10000)
		# The next allocation must skip 10000 even though that row is inactive.
		self.assertEqual(_make_mapping(b.name).public_port, 10001)

	def test_pool_exhaustion_throws(self) -> None:
		# Pool is 10000-10002 = 3 ports; the 4th allocation has nowhere to go.
		for _ in range(3):
			_make_mapping(_new_vm().name)
		with self.assertRaises(frappe.ValidationError):
			_make_mapping(_new_vm().name)

	def test_pool_exhaustion_message_names_field_and_range(self) -> None:
		# The exhaustion throw must be actionable: it names the Atlas Settings field to
		# grow and the exhausted range, so the operator knows the fix is a pool grow (a
		# deliberate snapshot roll), not a transient error to retry.
		for _ in range(3):
			_make_mapping(_new_vm().name)
		with self.assertRaises(frappe.ValidationError) as ctx:
			allocate_port()
		message = str(ctx.exception)
		self.assertIn("tcp_port_pool", message)
		self.assertIn("10000-10002", message)

	def test_pool_is_fleet_wide(self) -> None:
		# Ports are unique fleet-wide; the name embeds the protocol + port.
		a, b = _new_vm(), _new_vm()
		first = _make_mapping(a.name)
		second = _make_mapping(b.name)
		self.assertEqual(first.public_port, 10000)
		self.assertEqual(second.public_port, 10001)  # fleet-wide unique
		self.assertEqual(first.name, "tcp-10000")
		self.assertEqual(second.name, "tcp-10001")

	def test_allocate_port_helper_respects_taken(self) -> None:
		vm = _new_vm()
		_make_mapping(vm.name)  # takes 10000
		self.assertEqual(allocate_port(), 10001)

	# --- pool parsing ------------------------------------------------------

	def test_port_pool_parses_range(self) -> None:
		frappe.db.set_single_value("Atlas Settings", "tcp_port_pool", "20000-60000")
		self.assertEqual(port_pool(), (20000, 60000))

	def test_port_pool_rejects_garbage(self) -> None:
		frappe.db.set_single_value("Atlas Settings", "tcp_port_pool", "not-a-range")
		with self.assertRaises(frappe.ValidationError):
			port_pool()

	def test_port_pool_rejects_inverted_range(self) -> None:
		frappe.db.set_single_value("Atlas Settings", "tcp_port_pool", "19999-10000")
		with self.assertRaises(frappe.ValidationError):
			port_pool()

	# --- denormalization + immutability ------------------------------------

	def test_address_is_denormalized_from_vm(self) -> None:
		vm = _new_vm()
		mapping = _make_mapping(vm.name)
		self.assertEqual(mapping.address, vm.ipv6_address)
		self.assertTrue(mapping.address)

	def test_target_vm_must_be_addressable(self) -> None:
		vm = _new_vm()
		frappe.db.set_value("Virtual Machine", vm.name, "ipv6_address", None)
		with self.assertRaises(frappe.ValidationError):
			_make_mapping(vm.name)

	def test_target_vm_is_immutable(self) -> None:
		vm, other = _new_vm(), _new_vm()
		mapping = _make_mapping(vm.name)
		mapping.virtual_machine = other.name
		with self.assertRaises(frappe.ValidationError):
			mapping.save(ignore_permissions=True)

	def test_target_port_is_immutable(self) -> None:
		vm = _new_vm()
		mapping = _make_mapping(vm.name, target_port=22)
		mapping.target_port = 3306
		with self.assertRaises(frappe.ValidationError):
			mapping.save(ignore_permissions=True)

	def test_active_toggles_without_error(self) -> None:
		vm = _new_vm()
		mapping = _make_mapping(vm.name)
		mapping.active = 0
		mapping.save(ignore_permissions=True)
		self.assertFalse(mapping.active)

	# --- the desired map ---------------------------------------------------

	def test_port_map_builds_dialable_literals(self) -> None:
		vm = _new_vm()
		mapping = _make_mapping(vm.name, target_port=3306)
		mapped = port_map()
		# Value is a ready-to-dial bracketed-v6 host:port literal; key is the port
		# as a STRING (JSON object key, byte-identical to the guest's serializer).
		self.assertEqual(
			mapped,
			{str(mapping.public_port): f"[{vm.ipv6_address}]:3306"},
		)

	def test_port_map_returns_active_only(self) -> None:
		a, b = _new_vm(), _new_vm()
		active = _make_mapping(a.name)
		_make_mapping(b.name, active=0)
		mapped = port_map()
		self.assertEqual(set(mapped), {str(active.public_port)})

	def test_empty_when_no_active_mappings(self) -> None:
		self.assertEqual(port_map(), {})

	# --- reconcile enqueue -------------------------------------------------

	def test_insert_enqueues_reconcile(self) -> None:
		vm = _new_vm()
		with patch("frappe.enqueue") as enqueue:
			_make_mapping(vm.name)
		enqueue.assert_called_once()
		self.assertEqual(
			enqueue.call_args.args[0],
			"atlas.atlas.doctype.port_mapping.port_mapping.tcp_reconcile",
		)
		kwargs = enqueue.call_args.kwargs
		# Deduplicated + after-commit, exactly like Subdomain.
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["job_id"], "tcp_reconcile_ports")
		self.assertTrue(kwargs["enqueue_after_commit"])

	def test_active_toggle_enqueues_reconcile(self) -> None:
		vm = _new_vm()
		mapping = _make_mapping(vm.name)
		with patch("frappe.enqueue") as enqueue:
			mapping.active = 0
			mapping.save(ignore_permissions=True)
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["job_id"], "tcp_reconcile_ports")

	def test_save_without_active_change_does_not_reconcile(self) -> None:
		vm = _new_vm()
		mapping = _make_mapping(vm.name)
		with patch("frappe.enqueue") as enqueue:
			mapping.save(ignore_permissions=True)
		enqueue.assert_not_called()

	def test_delete_enqueues_reconcile(self) -> None:
		vm = _new_vm()
		mapping = _make_mapping(vm.name)
		with patch("frappe.enqueue") as enqueue:
			frappe.delete_doc("Port Mapping", mapping.name, ignore_permissions=True)
		reconciles = [
			call
			for call in enqueue.call_args_list
			if call.args and call.args[0] == "atlas.atlas.doctype.port_mapping.port_mapping.tcp_reconcile"
		]
		self.assertEqual(len(reconciles), 1)
		self.assertEqual(reconciles[0].kwargs["job_id"], "tcp_reconcile_ports")
