"""Tenant DocType: team-name naming and the resource-listing helpers.

Tenant is Central-facing: Central creates it named by its `Team.name` (passed as
`team`), then stamps the set-only-once `tenant` link on the resources it
provisions. It carries no identity beyond that key — Central performs every
permission check; the tenant is just the tag that groups a team's resources
(its VPC). These tests pin:

1. `autoname()` names the tenant by its `team`; the `team` (the primary key)
   cannot collide.
2. `virtual_machines()` / `images()` / `snapshots()` return only this tenant's
   rows; `resources()` returns the combined dict.
3. A resource's `tenant` link is set-only-once (changing it after insert throws).
"""

import frappe
from frappe.tests import IntegrationTestCase

from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine


def _make_tenant(team: str, **overrides) -> "frappe.model.document.Document":
	doc = {
		"doctype": "Tenant",
		"title": "Test Tenant",
		"team": team,
	}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def _ensure_test_server() -> str:
	provider = make_provider("tenant-test-provider")
	server = make_server(
		provider,
		"tenant-test-server",
		ipv4_address="10.0.0.98",
		ipv6_address="2001:db8:2::1",
		ipv6_prefix="2001:db8:2::/64",
		ipv6_virtual_machine_range="2001:db8:2::/124",
		status="Active",
	)
	return server.name


class TestTenant(IntegrationTestCase):
	def setUp(self) -> None:
		# Clear tenants and VMs from prior runs so uniqueness/range checks are clean.
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Tenant", pluck="name"):
			frappe.delete_doc("Tenant", name, force=1, ignore_permissions=True)

	def test_autoname_uses_team(self) -> None:
		tenant = _make_tenant("cust_a")
		self.assertEqual(tenant.name, "cust_a")

	def test_missing_team_throws(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Tenant", "title": "No Team"}).insert(ignore_permissions=True)

	def test_team_collision_rejected(self) -> None:
		# The team is the primary key, so a second tenant with the same team collides
		# on `name` (no separate unique field needed).
		_make_tenant("cust_same")
		with self.assertRaises(frappe.exceptions.DuplicateEntryError):
			_make_tenant("cust_same")

	def test_helpers_scope_to_this_tenant(self) -> None:
		server = _ensure_test_server()
		image = make_image("tenant-test-image")
		mine = _make_tenant("cust_mine")
		other = _make_tenant("cust_other")

		my_vm = make_virtual_machine(server, image, title="my vm", tenant=mine.name)
		make_virtual_machine(server, image, title="other vm", tenant=other.name)

		vms = mine.virtual_machines()
		self.assertEqual([v["name"] for v in vms], [my_vm.name])

		resources = mine.resources()
		self.assertEqual({"virtual_machines", "images", "snapshots"}, set(resources))
		self.assertEqual([v["name"] for v in resources["virtual_machines"]], [my_vm.name])

	def test_resource_tenant_is_set_only_once(self) -> None:
		server = _ensure_test_server()
		image = make_image("tenant-test-image")
		first = _make_tenant("cust_first")
		second = _make_tenant("cust_second")

		vm = make_virtual_machine(server, image, tenant=first.name)
		vm.tenant = second.name
		with self.assertRaises(frappe.ValidationError):
			vm.save(ignore_permissions=True)

	def test_tenant_stamped_from_create_payload(self) -> None:
		# The Central contract (spec/16-central.md): Central drives a VM create as
		# a service user and passes the target `tenant` as a field in the insert
		# payload — no bespoke endpoint. Pin that the field persists verbatim
		# through a plain insert (the path the SPA / run_doc_method / Central all
		# share), reloaded from the DB rather than read off the in-memory doc.
		server = _ensure_test_server()
		image = make_image("tenant-test-image")
		tenant = _make_tenant("cust_payload")

		vm = make_virtual_machine(server, image, tenant=tenant.name)
		self.assertEqual(
			frappe.db.get_value("Virtual Machine", vm.name, "tenant"),
			tenant.name,
			"tenant supplied in the create payload is persisted",
		)
