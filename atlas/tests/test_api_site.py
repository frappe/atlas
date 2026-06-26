"""Unit tests for the Central-facing site API (atlas.atlas.api.site).

`create_site` is the write half of the Central↔Atlas site contract: Central calls
it as the operator (token auth) to provision a self-serve site for a tenant. It
get-or-creates the Tenant, inserts the Site (Pending), and returns the mirror row
Central reflects. `get_site` is the read/poll half. All milliseconds, no host:
inserting the Site enqueues auto_provision but does NOT run it (frappe.in_test
queues without executing), so no VM is cloned — the Central contract (Tenant
stamping, mirror shape, region default, label gating) is what's pinned here. The
clone→deploy→route chain is proven in the self_serve_site e2e.
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api import site as site_api

ROOT_DOMAIN = "blr1.frappe.dev"
REGION = "blr1"

TEAM = "team-acme"
TENANT_EMAIL = "owner@acme.example.com"


def _ensure_root_domain() -> None:
	frappe.db.set_single_value("Atlas Settings", "region", REGION)
	frappe.db.set_single_value("Atlas Settings", "dns_provider_type", "Route53")
	frappe.db.set_single_value("Atlas Settings", "tls_provider_type", "Let's Encrypt")
	if not frappe.db.exists("Root Domain", ROOT_DOMAIN):
		frappe.get_doc(
			{
				"doctype": "Root Domain",
				"domain": ROOT_DOMAIN,
				"region": REGION,
				"is_active": 1,
				"dns_provider_type": "Route53",
				"tls_provider_type": "Let's Encrypt",
			}
		).insert(ignore_permissions=True)
	frappe.db.set_value("Root Domain", ROOT_DOMAIN, "is_active", 1)
	for name in frappe.get_all("Root Domain", filters={"is_active": 1}, pluck="name"):
		if name != ROOT_DOMAIN:
			frappe.db.set_value("Root Domain", name, "is_active", 0)


def _clear() -> None:
	for name in frappe.get_all("Site", pluck="name"):
		frappe.delete_doc("Site", name, force=1, ignore_permissions=True)
	if frappe.db.exists("Tenant", TEAM):
		frappe.delete_doc("Tenant", TEAM, force=1, ignore_permissions=True)


class TestCreateSite(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_root_domain()
		_clear()
		self.addCleanup(frappe.set_user, "Administrator")

	def test_creates_tenant_and_site(self) -> None:
		result = site_api.create_site(team=TEAM, subdomain="acme", email=TENANT_EMAIL)
		self.assertEqual(result["name"], "acme.blr1.frappe.dev")
		self.assertEqual(result["fqdn"], "acme.blr1.frappe.dev")
		self.assertEqual(result["status"], "Pending")
		self.assertEqual(result["team"], TEAM)
		# The Site is stamped with the get-or-created Tenant.
		tenant = frappe.db.get_value("Site", result["name"], "tenant")
		# The Tenant `name` *is* the Central `Team.name`.
		self.assertEqual(tenant, TEAM)
		self.assertEqual(frappe.db.get_value("Tenant", tenant, "email"), TENANT_EMAIL)

	def test_reuses_existing_tenant(self) -> None:
		"""A second site for the same Central team reuses the one Tenant (no email
		needed the second time — it is immutable after first creation)."""
		first = site_api.create_site(team=TEAM, subdomain="acme", email=TENANT_EMAIL)
		second = site_api.create_site(team=TEAM, subdomain="acme2")
		t1 = frappe.db.get_value("Site", first["name"], "tenant")
		t2 = frappe.db.get_value("Site", second["name"], "tenant")
		self.assertEqual(t1, t2)
		self.assertEqual(frappe.db.count("Tenant", {"name": TEAM}), 1)

	def test_region_defaults_to_active(self) -> None:
		result = site_api.create_site(team=TEAM, subdomain="acme", email=TENANT_EMAIL)
		self.assertEqual(result["region"], REGION)
		self.assertEqual(frappe.db.get_value("Site", result["name"], "region"), REGION)

	def test_new_tenant_without_email_throws(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			site_api.create_site(team=TEAM, subdomain="acme")
		self.assertIn("email is required", str(raised.exception))

	def test_missing_team_throws(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			site_api.create_site(team="", subdomain="acme", email=TENANT_EMAIL)
		self.assertIn("team is required", str(raised.exception))

	def test_reserved_label_throws(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			site_api.create_site(team=TEAM, subdomain="www", email=TENANT_EMAIL)
		self.assertIn("reserved", str(raised.exception))

	def test_duplicate_subdomain_throws_clean_taken(self) -> None:
		site_api.create_site(team=TEAM, subdomain="acme", email=TENANT_EMAIL)
		with self.assertRaises(frappe.ValidationError) as raised:
			site_api.create_site(team=TEAM, subdomain="acme")
		self.assertIn("already taken", str(raised.exception))


class TestGetSite(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_root_domain()
		_clear()
		self.addCleanup(frappe.set_user, "Administrator")

	def test_pending_site_hides_handoff(self) -> None:
		"""Before Running there is no admin handoff to surface — url + admin_password
		are None, status reflects the live row."""
		created = site_api.create_site(team=TEAM, subdomain="acme", email=TENANT_EMAIL)
		got = site_api.get_site(created["name"])
		self.assertEqual(got["status"], "Pending")
		self.assertIsNone(got["url"])
		self.assertIsNone(got["admin_password"])
		self.assertEqual(got["team"], TEAM)

	def test_running_site_reveals_handoff(self) -> None:
		"""Once Running, get_site surfaces the live URL + the stored admin password —
		the tenant handoff Central polls for."""
		created = site_api.create_site(team=TEAM, subdomain="acme", email=TENANT_EMAIL)
		site = frappe.get_doc("Site", created["name"])
		site.db_set("admin_password", "atlas-baked")
		site.db_set("status", "Running")
		got = site_api.get_site(created["name"])
		self.assertEqual(got["status"], "Running")
		self.assertEqual(got["url"], f"https://{created['name']}")
		self.assertEqual(got["admin_password"], "atlas-baked")
