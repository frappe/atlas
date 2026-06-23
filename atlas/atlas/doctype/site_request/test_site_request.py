"""Unit tests for the Site Request controller — the signup→verify→fulfil ordering
(Contract C), the shared Contract-A label validation, token + expiry, and owner
stamping. All milliseconds, no host: inserting the Site in a test enqueues
auto_provision but does NOT run it (frappe.in_test queues without executing), so
no VM is cloned — exactly the boundary spec/14-self-serve.md wants asserted.

The fulfilment ordering is THE invariant: a `Site` (and therefore a billable VM)
must exist only AFTER verification, never before."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date

from atlas.atlas.doctype.site_request import site_request as sr_module
from atlas.tests.fixtures import make_provider, make_server

ROOT_DOMAIN = "blr1.frappe.dev"
REGION = "blr1"

USER_EMAIL = "atlas-signup-user@example.com"
OTHER_EMAIL = "atlas-signup-other@example.com"


def _ensure_atlas_user_role() -> None:
	if not frappe.db.exists("Role", "Atlas User"):
		frappe.get_doc({"doctype": "Role", "role_name": "Atlas User", "desk_access": 0}).insert(
			ignore_permissions=True
		)
	# The role fixture is desk_access=0 (a fulfilled user is a Website User, off
	# Desk); prior test-DB runs can leave it drifted to 1, which would flip the
	# new User to System User. Pin it to the intended value for this test.
	frappe.db.set_value("Role", "Atlas User", "desk_access", 0)


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


def _clear() -> None:
	for name in frappe.get_all("Site Request", pluck="name"):
		frappe.delete_doc("Site Request", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Site", pluck="name"):
		frappe.delete_doc("Site", name, force=1, ignore_permissions=True)
	# Fulfilment creates Users, whose inserts can commit past the test transaction
	# rollback; delete the test users so each method starts clean (no PK clash).
	for email in (USER_EMAIL, OTHER_EMAIL):
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=1, ignore_permissions=True)


def _new_request(email: str = USER_EMAIL, subdomain: str = "acme", **overrides):
	doc = {"doctype": "Site Request", "email": email, "subdomain": subdomain}
	doc.update(overrides)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


class TestSiteRequestCreation(IntegrationTestCase):
	"""before_insert: shared Contract-A validation, region resolution, token, Pending."""

	def setUp(self) -> None:
		_ensure_root_domain()
		_clear()

	def test_starts_pending_with_token(self) -> None:
		request = _new_request()
		self.assertEqual(request.status, "Pending")
		self.assertTrue(request.token)
		self.assertEqual(len(request.token), 32)

	def test_region_resolved_from_active_root_domain(self) -> None:
		self.assertEqual(_new_request().region, REGION)

	def test_subdomain_normalized(self) -> None:
		self.assertEqual(_new_request(subdomain="  acme  ").subdomain, "acme")

	def test_rejects_dotted_label(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			_new_request(subdomain="ac.me")
		self.assertIn("single label", str(raised.exception))

	def test_rejects_uppercase_label(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			_new_request(subdomain="Acme")
		self.assertIn("lowercase", str(raised.exception))

	def test_rejects_reserved_label(self) -> None:
		with self.assertRaises(frappe.ValidationError) as raised:
			_new_request(subdomain="admin")
		self.assertIn("reserved", str(raised.exception))

	def test_uses_same_reserved_set_as_site(self) -> None:
		"""The shared helper is the one source of truth — Site re-exports it."""
		from atlas.atlas.doctype.site.site import RESERVED_SUBDOMAINS as site_set
		from atlas.atlas.subdomain_label import RESERVED_SUBDOMAINS as shared_set

		self.assertIs(site_set, shared_set)


class TestSiteRequestFulfilment(IntegrationTestCase):
	"""verify(): the ordering invariant (no Site before verification), User
	creation, owner stamping (Contract C), Fulfilled transition, idempotency."""

	def setUp(self) -> None:
		_ensure_atlas_user_role()
		_ensure_root_domain()
		# A snapshot must exist for Site.before_insert placement to resolve, but the
		# Site's auto_provision is enqueued (not run) in tests, so no VM is cloned.
		self._ensure_golden_snapshot()
		_clear()
		self.addCleanup(frappe.set_user, "Administrator")

	def _ensure_golden_snapshot(self) -> str:
		provider = make_provider("sr-test-provider")
		server = make_server(
			provider,
			"sr-test-server",
			ipv6_address="2001:db8:a::1",
			ipv6_prefix="2001:db8:a::/64",
			ipv6_virtual_machine_range="2001:db8:a::/124",
		)
		name = "golden-bench-sr"
		if not frappe.db.exists("Virtual Machine Snapshot", name):
			from atlas.tests.fixtures import make_image, make_virtual_machine

			image = make_image("sr-test-image")
			source_vm = make_virtual_machine(server, image, title="golden-source-sr")
			frappe.get_doc(
				{
					"doctype": "Virtual Machine Snapshot",
					"__newname": name,
					"title": "golden bench sr",
					"virtual_machine": source_vm.name,
					"server": server.name,
					"status": "Available",
					"source_image": image.name,
					"disk_gigabytes": 12,
					"rootfs_path": "/dev/atlas/atlas-snap-golden-sr",
				}
			).insert(ignore_permissions=True)
		frappe.db.set_single_value("Atlas Settings", "default_bench_snapshot", name)
		frappe.db.set_single_value("Atlas Settings", "ssh_public_key", "ssh-ed25519 AAAAFLEET")
		return name

	def test_no_site_exists_before_verification(self) -> None:
		"""THE invariant: a Pending request has created no Site (and so no VM)."""
		_new_request()
		self.assertEqual(frappe.db.count("Site"), 0)

	def test_verify_creates_site_owned_by_verified_user(self) -> None:
		request = _new_request()
		self.assertEqual(frappe.db.count("Site"), 0)  # nothing yet

		site = request.verify()

		self.assertEqual(site.name, "acme.blr1.frappe.dev")
		self.assertEqual(site.owner, USER_EMAIL)  # Contract C: verified user owns it
		request.reload()
		self.assertEqual(request.status, "Fulfilled")
		self.assertEqual(request.site, site.name)
		self.assertTrue(request.verified_at)
		self.assertEqual(request.owner, USER_EMAIL)  # request re-owned too

	def test_verify_creates_atlas_user(self) -> None:
		request = _new_request()
		request.verify()
		self.assertTrue(frappe.db.exists("User", USER_EMAIL))
		user = frappe.get_doc("User", USER_EMAIL)
		self.assertEqual(user.user_type, "Website User")
		self.assertIn("Atlas User", {row.role for row in user.get("roles")})

	def test_verify_reuses_existing_user(self) -> None:
		"""Account-light model: an existing User for the email is reused (one
		account, more Sites later), not duplicated."""
		existing = frappe.get_doc(
			{
				"doctype": "User",
				"email": USER_EMAIL,
				"first_name": "Already",
				"user_type": "Website User",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		request = _new_request()
		site = request.verify()
		self.assertEqual(site.owner, existing.name)
		self.assertEqual(frappe.db.count("User", {"email": USER_EMAIL}), 1)

	def test_verify_is_idempotent(self) -> None:
		"""A double-clicked link returns the same Site, does not provision twice."""
		request = _new_request()
		site = request.verify()
		again = request.verify()
		self.assertEqual(again.name, site.name)
		self.assertEqual(frappe.db.count("Site"), 1)

	def test_expired_token_is_rejected(self) -> None:
		request = _new_request()
		# Push creation past the TTL so is_expired() trips.
		old = add_to_date(None, hours=-(sr_module.TOKEN_TTL_HOURS + 1))
		frappe.db.set_value("Site Request", request.name, "creation", old, update_modified=False)
		request.reload()
		with self.assertRaises(frappe.ValidationError) as raised:
			request.verify()
		self.assertIn("expired", str(raised.exception))
		request.reload()
		self.assertEqual(request.status, "Expired")
		self.assertEqual(frappe.db.count("Site"), 0)  # nothing provisioned

	def test_session_user_restored_after_fulfilment(self) -> None:
		"""Fulfilment inserts the Site as the verified user but must leave the
		session user unchanged for the caller."""
		before = frappe.session.user
		_new_request().verify()
		self.assertEqual(frappe.session.user, before)


class TestSiteRequestPermissions(IntegrationTestCase):
	"""owner_only scoping — a user sees only their own requests (after fulfilment
	re-owns them)."""

	def setUp(self) -> None:
		_ensure_atlas_user_role()
		_ensure_root_domain()
		_clear()
		self.addCleanup(frappe.set_user, "Administrator")

	def _make_user(self, email: str) -> str:
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Sr",
					"user_type": "Website User",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		user = frappe.get_doc("User", email)
		if "Atlas User" not in {row.role for row in user.get("roles")}:
			user.append_roles("Atlas User")
			user.save(ignore_permissions=True)
		return user.name

	def test_user_lists_only_own_request(self) -> None:
		a = self._make_user(USER_EMAIL)
		b = self._make_user(OTHER_EMAIL)
		request = _new_request(email=USER_EMAIL)
		# Re-own to user A (mimics fulfilment's re-own without provisioning a Site).
		frappe.db.set_value("Site Request", request.name, "owner", a)

		frappe.set_user(b)
		names = {row.name for row in frappe.get_list("Site Request", limit_page_length=0)}
		self.assertNotIn(request.name, names)

		frappe.set_user(a)
		names = {row.name for row in frappe.get_list("Site Request", limit_page_length=0)}
		self.assertIn(request.name, names)
