from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.subdomain.subdomain import subdomain_map
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)


def _purge() -> None:
	for name in frappe.get_all("Subdomain", pluck="name"):
		frappe.delete_doc("Subdomain", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


def _make_subdomain(subdomain: str, vm: str, **overrides):
	doc = {
		"doctype": "Subdomain",
		"subdomain": subdomain,
		"virtual_machine": vm,
	}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


class TestSubdomain(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def test_address_is_denormalized_from_vm(self) -> None:
		vm = _new_vm()
		sub = _make_subdomain("acme", vm.name)
		# The proxy dials this literal; it must equal the target VM's /128 so the
		# desired-map query is a join-free SELECT.
		self.assertEqual(sub.address, vm.ipv6_address)
		self.assertTrue(sub.address)

	def test_autoname_is_the_subdomain(self) -> None:
		vm = _new_vm()
		sub = _make_subdomain("widgets", vm.name)
		self.assertEqual(sub.name, "widgets")

	def test_subdomain_is_unique(self) -> None:
		vm = _new_vm()
		_make_subdomain("acme", vm.name)
		with self.assertRaises(frappe.exceptions.DuplicateEntryError):
			_make_subdomain("acme", vm.name)

	def test_target_vm_must_be_addressable(self) -> None:
		vm = _new_vm()
		# Strip the VM's address: an unaddressable target can't be a route.
		frappe.db.set_value("Virtual Machine", vm.name, "ipv6_address", None)
		with self.assertRaises(frappe.ValidationError):
			_make_subdomain("acme", vm.name)

	def test_routing_key_and_target_are_immutable(self) -> None:
		vm = _new_vm()
		other = _new_vm()
		sub = _make_subdomain("acme", vm.name)
		sub.virtual_machine = other.name
		with self.assertRaises(frappe.ValidationError):
			sub.save(ignore_permissions=True)

	def test_active_toggles_without_error(self) -> None:
		vm = _new_vm()
		sub = _make_subdomain("acme", vm.name)
		sub.active = 0
		sub.save(ignore_permissions=True)
		self.assertFalse(sub.active)

	def test_subdomain_map_returns_active_only(self) -> None:
		vm_a = _new_vm()
		vm_b = _new_vm()
		_make_subdomain("acme", vm_a.name)
		_make_subdomain("widgets", vm_b.name)
		_make_subdomain("dormant", vm_a.name, active=0)
		site_map = subdomain_map()
		self.assertEqual(
			site_map,
			{"acme": vm_a.ipv6_address, "widgets": vm_b.ipv6_address},
		)
		self.assertNotIn("dormant", site_map)

	def test_empty_map_when_no_active_subdomains(self) -> None:
		self.assertEqual(subdomain_map(), {})

	def test_insert_enqueues_reconcile(self) -> None:
		vm = _new_vm()
		with patch("frappe.enqueue") as enqueue:
			_make_subdomain("acme", vm.name)
		enqueue.assert_called_once()
		_, kwargs = enqueue.call_args
		self.assertEqual(
			enqueue.call_args.args[0],
			"atlas.atlas.doctype.subdomain.subdomain.auto_reconcile",
		)
		# Deduplicated so a burst of subdomain changes collapses to one queued
		# reconcile (a wedged proxy makes each take its full SSH timeout, so
		# undeduped they flood `long` and starve everything else).
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["job_id"], "auto_reconcile_subdomains")
		# After-commit so the reconcile reads this change's committed map row.
		self.assertTrue(kwargs["enqueue_after_commit"])

	def test_active_toggle_enqueues_reconcile(self) -> None:
		vm = _new_vm()
		sub = _make_subdomain("acme", vm.name)
		with patch("frappe.enqueue") as enqueue:
			sub.active = 0
			sub.save(ignore_permissions=True)
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["job_id"], "auto_reconcile_subdomains")

	def test_save_without_active_change_does_not_reconcile(self) -> None:
		vm = _new_vm()
		sub = _make_subdomain("acme", vm.name)
		# A no-op save (no map-affecting field changed) must not SSH the fleet.
		with patch("frappe.enqueue") as enqueue:
			sub.save(ignore_permissions=True)
		enqueue.assert_not_called()

	def test_delete_enqueues_reconcile(self) -> None:
		vm = _new_vm()
		sub = _make_subdomain("acme", vm.name)
		# delete_doc itself enqueues unrelated jobs (delete_dynamic_links), so pick
		# out our reconcile call rather than asserting a single enqueue.
		with patch("frappe.enqueue") as enqueue:
			frappe.delete_doc("Subdomain", sub.name, ignore_permissions=True)
		reconciles = [
			call
			for call in enqueue.call_args_list
			if call.args and call.args[0] == "atlas.atlas.doctype.subdomain.subdomain.auto_reconcile"
		]
		self.assertEqual(len(reconciles), 1)
		self.assertEqual(reconciles[0].kwargs["job_id"], "auto_reconcile_subdomains")
