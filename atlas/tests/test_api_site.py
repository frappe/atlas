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

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api import site as site_api

ROOT_DOMAIN = "blr1.frappe.dev"
REGION = "blr1"

TEAM = "team-acme"


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
		result = site_api.create_site(team=TEAM, subdomain="acme")
		self.assertEqual(result["name"], "acme.blr1.frappe.dev")
		self.assertEqual(result["fqdn"], "acme.blr1.frappe.dev")
		self.assertEqual(result["status"], "Pending")
		self.assertEqual(result["team"], TEAM)
		# The Site is stamped with the get-or-created Tenant.
		tenant = frappe.db.get_value("Site", result["name"], "tenant")
		# The Tenant `name` *is* the Central `Team.name`.
		self.assertEqual(tenant, TEAM)

	def test_reuses_existing_tenant(self) -> None:
		"""A second site for the same Central team reuses the one Tenant (keyed on
		`team`, the Central `Team.name`)."""
		first = site_api.create_site(team=TEAM, subdomain="acme")
		second = site_api.create_site(team=TEAM, subdomain="acme2")
		t1 = frappe.db.get_value("Site", first["name"], "tenant")
		t2 = frappe.db.get_value("Site", second["name"], "tenant")
		self.assertEqual(t1, t2)
		self.assertEqual(frappe.db.count("Tenant", {"name": TEAM}), 1)

	def test_missing_team_throws(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			site_api.create_site(team="", subdomain="acme")
		self.assertIn("team is required", str(raised.exception))

	def test_reserved_label_throws(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			site_api.create_site(team=TEAM, subdomain="www")
		self.assertIn("reserved", str(raised.exception))

	def test_duplicate_subdomain_throws_clean_taken(self) -> None:
		site_api.create_site(team=TEAM, subdomain="acme")
		with self.assertRaises(frappe.ValidationError) as raised:
			site_api.create_site(team=TEAM, subdomain="acme")
		self.assertIn("already taken", str(raised.exception))

	def test_bench_seed_rides_the_provision_job_not_the_site(self) -> None:
		"""The pilot enrollment seed is bench-level: create_site threads it into the provision
		job (→ VM + bench.toml), never onto the Site row, and never into _mirror."""
		with patch("frappe.enqueue") as enqueue:
			result = site_api.create_site(
				team=TEAM,
				subdomain="acme",
				pilot_credential_id="pcred-abc",
				central_endpoint="https://central.test",
				bootstrap_token="boot-token",
			)
		job = next(c.kwargs for c in enqueue.call_args_list if c.args and "auto_provision" in c.args[0])
		self.assertEqual(job["pilot_credential_id"], "pcred-abc")
		self.assertEqual(job["central_endpoint"], "https://central.test")
		self.assertEqual(job["bootstrap_token"], "boot-token")
		# Nothing central-flavoured is persisted on the Site, and the token never leaks
		# into the mirror Central reflects.
		self.assertNotIn("bootstrap_token", result)
		self.assertFalse(frappe.get_meta("Site").get_field("bootstrap_token"))


class TestGetSite(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_root_domain()
		_clear()
		self.addCleanup(frappe.set_user, "Administrator")

	def test_pending_site_hides_handoff(self) -> None:
		"""Before Running there is no admin handoff to surface — url + login_url
		are None, status reflects the live row."""
		created = site_api.create_site(team=TEAM, subdomain="acme")
		got = site_api.get_site(created["name"])
		self.assertEqual(got["status"], "Pending")
		self.assertIsNone(got["url"])
		self.assertIsNone(got["login_url"])
		self.assertIsNone(got["login_url_expires_at"])
		self.assertEqual(got["team"], TEAM)

	def test_running_site_reveals_handoff(self) -> None:
		"""Once Running, get_site surfaces the live URL + the stored login URL +
		its expiry — the tenant handoff Central polls for."""
		created = site_api.create_site(team=TEAM, subdomain="acme")
		site = frappe.get_doc("Site", created["name"])
		login_url = f"https://{created['name']}/app?sid=abc123"
		expires_at = "2026-07-02 12:00:00"
		site.db_set("login_url", login_url)
		site.db_set("login_url_expires_at", expires_at)
		site.db_set("status", "Running")
		got = site_api.get_site(created["name"])
		self.assertEqual(got["status"], "Running")
		self.assertEqual(got["url"], f"https://{created['name']}")
		self.assertEqual(got["login_url"], login_url)
		self.assertEqual(
			frappe.utils.get_datetime(got["login_url_expires_at"]), frappe.utils.get_datetime(expires_at)
		)
