"""Doctype tests for Custom Domain (spec/18 Phase 2, the SNI-passthrough custom-domain
layer) — the full-FQDN sibling of Subdomain.

Covers, with no host: address denormalization, the full-FQDN autoname + uniqueness,
routing-key/target immutability, the two maps (custom_domain_sni_map and
custom_domain_acme_map each carry every active row — there is no readiness gate), and the
insert/active-toggle/delete reconcile hooks sharing the dedup subdomain job.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.custom_domain.custom_domain import (
	custom_domain_acme_map,
	custom_domain_sni_map,
)
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)

RECONCILE_JOB = "auto_reconcile_subdomains"
RECONCILE_METHOD = "atlas.atlas.doctype.subdomain.subdomain.auto_reconcile"


def _purge() -> None:
	for name in frappe.get_all("Custom Domain", pluck="name"):
		frappe.delete_doc("Custom Domain", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


def _make_custom_domain(domain: str, vm: str, **overrides):
	doc = {
		"doctype": "Custom Domain",
		"domain": domain,
		"virtual_machine": vm,
		"status": "Active",
		"active": 1,
	}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


class TestCustomDomain(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge()

	def test_address_is_denormalized_from_vm(self) -> None:
		vm = _new_vm()
		cd = _make_custom_domain("shop.acme.com", vm.name)
		self.assertEqual(cd.address, vm.ipv6_address)
		self.assertTrue(cd.address)

	def test_autoname_is_the_full_domain(self) -> None:
		vm = _new_vm()
		cd = _make_custom_domain("shop.acme.com", vm.name)
		self.assertEqual(cd.name, "shop.acme.com")

	def test_domain_is_unique(self) -> None:
		vm = _new_vm()
		_make_custom_domain("shop.acme.com", vm.name)
		with self.assertRaises(frappe.exceptions.DuplicateEntryError):
			_make_custom_domain("shop.acme.com", vm.name)

	def test_target_vm_must_be_addressable(self) -> None:
		vm = _new_vm()
		frappe.db.set_value("Virtual Machine", vm.name, "ipv6_address", None)
		with self.assertRaises(frappe.ValidationError):
			_make_custom_domain("shop.acme.com", vm.name)

	def test_routing_key_and_target_are_immutable(self) -> None:
		vm = _new_vm()
		other = _new_vm()
		cd = _make_custom_domain("shop.acme.com", vm.name)
		cd.virtual_machine = other.name
		with self.assertRaises(frappe.ValidationError):
			cd.save(ignore_permissions=True)

	def test_status_toggles_without_error(self) -> None:
		vm = _new_vm()
		cd = _make_custom_domain("shop.acme.com", vm.name)
		cd.status = "Failed"
		cd.save(ignore_permissions=True)
		self.assertEqual(cd.status, "Failed")

	# --- the two maps (every active row, no readiness gate) -----------------------

	def test_sni_map_carries_every_active_row(self) -> None:
		# The :443 SNI map carries EVERY active row — there is no readiness gate; a domain is
		# in the SNI passthrough map the moment it is registered. Value is the [v6]:443 literal.
		vm_a = _new_vm()
		vm_b = _new_vm()
		_make_custom_domain("shop.acme.com", vm_a.name)
		_make_custom_domain("blog.acme.com", vm_b.name)
		sni = custom_domain_sni_map()
		self.assertEqual(
			sni,
			{
				"shop.acme.com": f"[{vm_a.ipv6_address}]:443",
				"blog.acme.com": f"[{vm_b.ipv6_address}]:443",
			},
		)

	def test_acme_map_carries_every_active_row(self) -> None:
		# The :80 ACME map carries EVERY active row so a VM can issue. Value is the bare
		# bracketed v6 (acme_router appends :80) — the same row set as the SNI map.
		vm_a = _new_vm()
		vm_b = _new_vm()
		_make_custom_domain("shop.acme.com", vm_a.name)
		_make_custom_domain("blog.acme.com", vm_b.name)
		acme = custom_domain_acme_map()
		self.assertEqual(
			acme,
			{"shop.acme.com": f"[{vm_a.ipv6_address}]", "blog.acme.com": f"[{vm_b.ipv6_address}]"},
		)

	def test_inactive_rows_excluded_from_both_maps(self) -> None:
		vm = _new_vm()
		_make_custom_domain("shop.acme.com", vm.name, status="Active", active=0)
		self.assertEqual(custom_domain_sni_map(), {})
		self.assertEqual(custom_domain_acme_map(), {})

	def test_empty_maps_when_no_custom_domains(self) -> None:
		self.assertEqual(custom_domain_sni_map(), {})
		self.assertEqual(custom_domain_acme_map(), {})

	# --- reconcile hooks (shared dedup job with Subdomain) ------------------------

	def test_insert_enqueues_shared_reconcile(self) -> None:
		vm = _new_vm()
		with patch("frappe.enqueue") as enqueue:
			_make_custom_domain("shop.acme.com", vm.name)
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.args[0], RECONCILE_METHOD)
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
		self.assertEqual(enqueue.call_args.kwargs["job_id"], RECONCILE_JOB)
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

	def test_active_toggle_enqueues_reconcile(self) -> None:
		vm = _new_vm()
		cd = _make_custom_domain("shop.acme.com", vm.name)
		with patch("frappe.enqueue") as enqueue:
			cd.active = 0
			cd.save(ignore_permissions=True)
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["job_id"], RECONCILE_JOB)

	def test_status_flip_does_not_reconcile(self) -> None:
		# `status` no longer drives the served maps (there is no readiness gate), so flipping
		# it (e.g. Active -> Failed) must NOT SSH the fleet — only `active` changes the maps.
		vm = _new_vm()
		cd = _make_custom_domain("shop.acme.com", vm.name)
		with patch("frappe.enqueue") as enqueue:
			cd.status = "Failed"
			cd.save(ignore_permissions=True)
		enqueue.assert_not_called()

	def test_true_noop_save_does_not_reconcile(self) -> None:
		# A save that does not change `active` must not SSH the fleet.
		vm = _new_vm()
		cd = _make_custom_domain("shop.acme.com", vm.name)
		with patch("frappe.enqueue") as enqueue:
			cd.save(ignore_permissions=True)
		enqueue.assert_not_called()

	def test_delete_enqueues_reconcile(self) -> None:
		vm = _new_vm()
		cd = _make_custom_domain("shop.acme.com", vm.name)
		with patch("frappe.enqueue") as enqueue:
			frappe.delete_doc("Custom Domain", cd.name, ignore_permissions=True)
		reconciles = [c for c in enqueue.call_args_list if c.args and c.args[0] == RECONCILE_METHOD]
		self.assertEqual(len(reconciles), 1)
		self.assertEqual(reconciles[0].kwargs["job_id"], RECONCILE_JOB)
