"""Placement defaults for Virtual Machines.

A VM is created with only name / size / SSH key; the controller fills `server`
and `image` in before_insert (atlas/atlas/placement.py). These tests pin that the
fill happens, that `owner` is stamped from the acting user, and that the
no-capacity / ambiguous-image boundaries throw cleanly. No host — pure controller
logic (the after_insert provision enqueue is a no-op under frappe.in_test).
"""

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.placement import NoCapacityError
from atlas.tests.fixtures import make_image, make_provider, make_server

USER_EMAIL = "atlas-placement-user@example.com"


def _acting_user() -> str:
	"""A plain enabled User to act as — placement is operator-agnostic, so the
	role no longer matters; the test only needs a distinct session user to assert
	`owner` is stamped from it."""
	if frappe.db.exists("User", USER_EMAIL):
		return USER_EMAIL
	return (
		frappe.get_doc(
			{
				"doctype": "User",
				"email": USER_EMAIL,
				"first_name": "Place",
				"last_name": "Ment",
				"send_welcome_email": 0,
				"enabled": 1,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


class TestPlacement(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider("atlas-placement-provider")
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.set_single_value("Atlas Settings", "default_user_image", None)
		# No oversubscription unless a test opts in; keeps capacity assertions
		# independent of suite order.
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 1)
		# Wipe VMs left by other tests: servers are shared by title, so a stray
		# VM on the reused server would count against its vCPU budget and skew
		# the capacity-boundary tests below.
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)
		# Start from a clean slate: placement picks the first Active server and
		# throws on >1 active image, so neutralize any left by other suites /
		# fixtures so this test's own server+image are the only candidates.
		for name in frappe.get_all("Virtual Machine Image", filters={"is_active": 1}, pluck="name"):
			frappe.db.set_value("Virtual Machine Image", name, "is_active", 0)
		for name in frappe.get_all("Server", filters={"status": "Active"}, pluck="name"):
			frappe.db.set_value("Server", name, "status", "Draining")

	def _new_machine(self, **overrides):
		"""Insert a VM the way the Central API does — no server, no image, and
		`ignore_permissions` (the real caller is operator orchestration authorized by
		the Central token, not desk RBAC). Frappe still stamps `owner` from the
		session user, so the owner-attribution assertion holds."""
		doc = {
			"doctype": "Virtual Machine",
			"title": "placement-vm",
			"size_preset": "Shared 1x",
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": "ssh-ed25519 AAAA",
		}
		doc.update(overrides)
		return frappe.get_doc(doc).insert(ignore_permissions=True)

	def test_fills_server_and_image_and_owner(self) -> None:
		# setUp drained every Active server, so this is the only candidate.
		# Give it generous capacity so placement's vCPU check can't be the thing
		# under test here (capacity is exercised by test_no_active_server_throws).
		server = make_server(
			self.provider,
			title="atlas-placement-server",
			size="DigitalOcean/s-4vcpu-8gb",
			ipv6_address="2001:db8:1::1",
			ipv6_prefix="2001:db8:1::/64",
			ipv6_virtual_machine_range="2001:db8:1::/124",
		)
		image = make_image("atlas-placement-image")
		# make_image returns an existing row if present; setUp may have just
		# deactivated it, so re-assert active for the single-image happy path.
		frappe.db.set_value("Virtual Machine Image", image.name, "is_active", 1)
		frappe.db.set_value("Server", server.name, "status", "Active")

		user = _acting_user()
		frappe.set_user(user)
		vm = self._new_machine()

		self.assertEqual(vm.server, server.name, "server filled from the only active server")
		self.assertEqual(vm.image, image.name, "image filled from the single active image")
		self.assertEqual(vm.owner, user, "owner is stamped from the acting user")
		self.assertTrue(vm.ipv6_address, "ipv6 allocated against the filled server")

	def test_explicit_server_image_not_overridden(self) -> None:
		server = make_server(
			self.provider,
			title="atlas-placement-server",
			ipv6_address="2001:db8:1::1",
			ipv6_prefix="2001:db8:1::/64",
			ipv6_virtual_machine_range="2001:db8:1::/124",
		)
		image = make_image("atlas-placement-image")
		frappe.db.set_value("Server", server.name, "status", "Active")
		# Operator path: both supplied — placement is a no-op.
		vm = self._new_machine(server=server.name, image=image.name)
		self.assertEqual(vm.server, server.name)
		self.assertEqual(vm.image, image.name)

	def test_no_active_server_throws(self) -> None:
		image = make_image("atlas-placement-image")
		# setUp deactivates every image; re-assert active so default_image()
		# resolves and the throw genuinely comes from the no-server branch (not
		# from image resolution running first in apply_user_defaults).
		frappe.db.set_value("Virtual Machine Image", image.name, "is_active", 1)
		# A server exists but is not Active.
		make_server(self.provider, title="atlas-placement-server")
		frappe.set_user(_acting_user())
		# Typed NoCapacityError (a ValidationError subclass) so Central can tell
		# "region full" apart from a bad request — spec/16-central.md.
		with self.assertRaises(NoCapacityError):
			self._new_machine()

	def _full_4vcpu_server(self):
		"""An Active 4-vCPU server already running 4 vCPUs of VMs, plus a single
		active image. Shared setup for the overprovisioning boundary tests."""
		server = make_server(
			self.provider,
			title="atlas-placement-server",
			size="DigitalOcean/s-4vcpu-8gb",
			ipv6_address="2001:db8:1::1",
			ipv6_prefix="2001:db8:1::/64",
			ipv6_virtual_machine_range="2001:db8:1::/124",
		)
		image = make_image("atlas-placement-image")
		frappe.db.set_value("Virtual Machine Image", image.name, "is_active", 1)
		frappe.db.set_value("Server", server.name, "status", "Active")
		frappe.set_user(_acting_user())
		self._new_machine(vcpus=4, memory_megabytes=512, disk_gigabytes=4)
		return server

	def test_full_server_throws_at_default_factor(self) -> None:
		# Default factor 1: a 4-vCPU server with 4 vCPUs used has no room.
		self._full_4vcpu_server()
		with self.assertRaises(NoCapacityError):
			self._new_machine()

	def test_overprovision_factor_opens_room_on_full_server(self) -> None:
		# A 16x factor lifts the budget to 64 effective vCPUs, so the same
		# fully-booked server now accepts the VM.
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 16)
		server = self._full_4vcpu_server()
		vm = self._new_machine()
		self.assertEqual(vm.server, server.name, "16x factor leaves room")

	def test_ambiguous_image_throws(self) -> None:
		server = make_server(
			self.provider,
			title="atlas-placement-server",
			ipv6_address="2001:db8:1::1",
			ipv6_prefix="2001:db8:1::/64",
			ipv6_virtual_machine_range="2001:db8:1::/124",
		)
		frappe.db.set_value("Server", server.name, "status", "Active")
		make_image("atlas-placement-image-a")
		make_image("atlas-placement-image-b")
		frappe.set_user(_acting_user())
		with self.assertRaises(frappe.ValidationError):
			self._new_machine()

	def test_configured_default_image_resolves_ambiguity(self) -> None:
		server = make_server(
			self.provider,
			title="atlas-placement-server",
			ipv6_address="2001:db8:1::1",
			ipv6_prefix="2001:db8:1::/64",
			ipv6_virtual_machine_range="2001:db8:1::/124",
		)
		frappe.db.set_value("Server", server.name, "status", "Active")
		make_image("atlas-placement-image-a")
		image_b = make_image("atlas-placement-image-b")
		frappe.db.set_single_value("Atlas Settings", "default_user_image", image_b.name)
		frappe.set_user(_acting_user())
		vm = self._new_machine()
		self.assertEqual(vm.image, image_b.name, "configured default wins over ambiguity")
