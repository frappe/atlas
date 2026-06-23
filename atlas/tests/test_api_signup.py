"""Unit tests for the public signup API (spec/14-self-serve.md). Asserts the guest-callable
`request_site`: Contract-A validation, the "taken" pre-check, the per-email
pending cap, that it creates a Pending Site Request (and NOTHING else — no User,
no Site, no VM, Contract C), and that it queues the verification email (asserted
via Frappe's outbox without real SMTP).

The IP-based rate-limit decorator wraps a request context that the unit harness
doesn't supply, so we exercise the inner logic by calling through the module; the
per-email cap (the abuse control that matters for fan-out) is tested directly."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api import signup as signup_module

ROOT_DOMAIN = "blr1.frappe.dev"
REGION = "blr1"
EMAIL = "atlas-api-signup@example.com"


def _ensure_root_domain() -> None:
	frappe.db.set_single_value("Route53 Settings", "domain_provider_type", "Route53", update_modified=False)
	frappe.db.set_single_value("Atlas Settings", "tls_provider_type", "Let's Encrypt", update_modified=False)
	if not frappe.db.exists("Root Domain", ROOT_DOMAIN):
		frappe.get_doc(
			{
				"doctype": "Root Domain",
				"domain": ROOT_DOMAIN,
				"region": REGION,
				"is_active": 1,
				"domain_provider_type": "Route53",
				"tls_provider_type": "Let's Encrypt",
			}
		).insert(ignore_permissions=True)
	frappe.db.set_value("Root Domain", ROOT_DOMAIN, "is_active", 1)
	for name in frappe.get_all("Root Domain", filters={"is_active": 1}, pluck="name"):
		if name != ROOT_DOMAIN:
			frappe.db.set_value("Root Domain", name, "is_active", 0)


def _request_site(email: str = EMAIL, subdomain: str = "acme") -> dict:
	# Call the undecorated implementation: the rate_limit decorator needs a real
	# request context (frappe.local.request) the unit harness doesn't build.
	return signup_module.request_site.__wrapped__(email=email, subdomain=subdomain)


def _ensure_outgoing_email_account() -> None:
	if not frappe.db.exists("Email Account", "Atlas Test Outgoing"):
		frappe.get_doc(
			{
				"doctype": "Email Account",
				"email_account_name": "Atlas Test Outgoing",
				"email_id": "atlas-test@example.com",
				"smtp_server": "localhost",
				"enable_outgoing": 1,
				"default_outgoing": 1,
			}
		).insert(ignore_permissions=True)
	else:
		frappe.db.set_value("Email Account", "Atlas Test Outgoing", "default_outgoing", 1)


class TestSignupRequestSite(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_root_domain()
		_ensure_outgoing_email_account()
		for name in frappe.get_all("Site Request", pluck="name"):
			frappe.delete_doc("Site Request", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Site", pluck="name"):
			frappe.delete_doc("Site", name, force=1, ignore_permissions=True)
		frappe.flags.mute_emails = False
		frappe.local.flags.in_test = True

	def test_creates_pending_request_only(self) -> None:
		"""Contract C: the API creates a Pending Site Request and nothing else —
		no User, no Site, no VM happen before verification."""
		result = _request_site()
		self.assertEqual(result["subdomain"], "acme")
		requests = frappe.get_all("Site Request", filters={"email": EMAIL}, fields=["status", "subdomain"])
		self.assertEqual(len(requests), 1)
		self.assertEqual(requests[0]["status"], "Pending")
		self.assertEqual(frappe.db.count("Site"), 0)
		self.assertFalse(frappe.db.exists("User", EMAIL))

	def test_queues_verification_email(self) -> None:
		_request_site()
		outbox = frappe.get_all(
			"Email Queue",
			filters={"reference_doctype": ["is", "not set"]},
			fields=["name", "message"],
			order_by="creation desc",
			limit=5,
		)
		# At least one queued mail mentions the FQDN and the verify link.
		messages = "\n".join((row.get("message") or "") for row in outbox)
		self.assertIn("acme.blr1.frappe.dev", messages)
		self.assertIn("/verify?token=", messages)

	def test_rejects_invalid_email(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			_request_site(email="not-an-email")
		self.assertIn("valid email", str(raised.exception))
		self.assertEqual(frappe.db.count("Site Request"), 0)

	def test_rejects_reserved_subdomain(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			_request_site(subdomain="admin")
		self.assertIn("reserved", str(raised.exception))
		self.assertEqual(frappe.db.count("Site Request"), 0)

	def test_rejects_dotted_subdomain(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			_request_site(subdomain="ac.me")
		self.assertEqual(frappe.db.count("Site Request"), 0)

	def test_rejects_taken_subdomain(self) -> None:
		"""A label already owned by a live Site is rejected at request time."""
		frappe.get_doc({"doctype": "Site", "subdomain": "acme"}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as raised:
			_request_site(subdomain="acme")
		self.assertIn("already taken", str(raised.exception))

	def test_per_email_pending_cap(self) -> None:
		"""An email can't mint unbounded Pending requests (fan-out abuse)."""
		for i in range(signup_module.MAX_PENDING_PER_EMAIL):
			_request_site(subdomain=f"acme{i}")
		with self.assertRaises(frappe.ValidationError) as raised:
			_request_site(subdomain="onemore")
		self.assertIn("in flight", str(raised.exception))
