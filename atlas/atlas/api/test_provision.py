"""Unit tests for the Central-facing VM API (atlas.atlas.api.provision).

`create_vm` is the write half of the Central↔Atlas VM contract: Central calls it
as the operator (token auth) to provision a tenant bench. The WIRE shape is
VM-shaped — Central mirrors a VM row — but behind it `create_vm` now creates a
`Pilot` that owns the backing VM (the bench provision lives on the Pilot, not the
VM). `title` doubles as the pilot subdomain; Atlas fronts it at `<title>.<region
domain>` (derived — the gateway_url the console deep-links). The plain VM facts
(name, ipv6) are read back through the VM the Pilot created; the bench fields
(gateway_url, login_url) through the Pilot.

Milliseconds, no host: a Fake-backed server means the VM inserts and the pilot's
gateway_url derives without shelling out. The login URL is minted after boot (the
pilot's background job), so it is empty in the create return — Central learns it
from the vm.status_changed event the pilot emits. The mint + regenerate are proven
in test_pilot.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api import provision as provision_api
from atlas.tests import fixtures

ROOT_DOMAIN = "blr1.frappe.dev"
REGION = "blr1"
TEAM = "team-acme"
TENANT_EMAIL = "owner@acme.example.com"


def _ensure_root_domain() -> None:
	frappe.db.set_single_value("Atlas Settings", "region", REGION)
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


class TestCreateVM(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_root_domain()
		self.provider = fixtures.make_provider_row("fake-test-provider", provider_type="Fake")
		fixtures.set_atlas_settings(self.provider, ssh_public_key="ssh-ed25519 AAAAFLEET")
		frappe.db.set_single_value("Atlas Settings", "region", REGION)
		frappe.db.set_single_value("Atlas Settings", "ssh_public_key", "ssh-ed25519 AAAAFLEET")
		self.server = self._make_server()
		self.admin_image = fixtures.make_image("fake-bench-admin-image", build_mode="admin")
		# create_vm's default-image resolution needs exactly one active image; pin the
		# bench admin image as the sole active one.
		frappe.db.set_value("Virtual Machine Image", self.admin_image.name, "is_active", 1)
		for name in frappe.get_all("Virtual Machine Image", filters={"is_active": 1}, pluck="name"):
			if name != self.admin_image.name:
				frappe.db.set_value("Virtual Machine Image", name, "is_active", 0)
		# create_vm no longer pins a hardcoded server — the Pilot places the VM on a
		# server that HOLDS the default image (placement.default_server_for_image). Give
		# the image a home on the fake server by recording a successful sync-image Task,
		# so placement finds a candidate instead of throwing "not present on any server".
		self._sync_image_to(self.admin_image.name, self.server.name)
		for name in frappe.get_all("Pilot", pluck="name"):
			frappe.delete_doc("Pilot", name, force=1, ignore_permissions=True)
		self.addCleanup(frappe.set_user, "Administrator")

	def _sync_image_to(self, image: str, server: str) -> None:
		"""Record a successful `sync-image` Task so `image` has a home on `server` — the
		presence signal placement.image_home_servers reads to pick a placement host."""
		import json

		frappe.get_doc(
			{
				"doctype": "Task",
				"server": server,
				"script": "sync-image",
				"variables": json.dumps({"IMAGE_NAME": image}),
				"status": "Success",
				"triggered_by": "Administrator",
			}
		).insert(ignore_permissions=True)

	def _make_server(self):
		"""A Fake-backed Active Server for the VM to land on. create_vm no longer pins a
		server; placement picks this one because setUp syncs the default image to it."""
		server = frappe.new_doc("Server")
		server.update(
			{
				"title": "fake-test-server",
				"provider_type": "Fake",
				"provider_resource_id": None,
				"size": fixtures.DEFAULT_DIGITALOCEAN_SIZE,
				"status": "Active",
				"ipv4_address": "203.0.113.10",
				"ipv6_address": "2001:db8:abcd::1",
				"ipv6_prefix": "2001:db8:abcd::/64",
				"ipv6_virtual_machine_range": "2001:db8:abcd::/124",
			}
		)
		return server.insert(ignore_permissions=True)

	def _create(self, title="acme"):
		return provision_api.create_vm(
			team=TEAM,
			title=title,
			vcpus=1,
			memory_megabytes=512,
			disk_gigabytes=2,
			email=TENANT_EMAIL,
			cpu_max_cores=None,
		)

	def test_creates_a_pilot_that_owns_the_vm(self) -> None:
		"""create_vm creates a Pilot, which creates + links the VM. The return is
		VM-shaped: the VM's real identity, the pilot's derived gateway_url."""
		result = self._create("acme")
		pilot = frappe.get_doc("Pilot", "acme.blr1.frappe.dev")
		self.assertTrue(pilot.tenant)  # the owning tenant was stamped from the team
		self.assertEqual(result["name"], pilot.virtual_machine)
		vm = frappe.get_doc("Virtual Machine", pilot.virtual_machine)
		self.assertEqual(vm.title, "acme")
		self.assertEqual(result["ipv6_address"], vm.ipv6_address)

	def test_gateway_url_is_the_derived_fqdn(self) -> None:
		result = self._create("acme")
		self.assertEqual(result["gateway_url"], "https://acme.blr1.frappe.dev")

	def test_login_url_is_empty_at_create(self) -> None:
		"""The login URL is minted after boot (the pilot's background job), so it is not
		in the create return — Central learns it from the pilot's vm.status_changed."""
		result = self._create("acme")
		self.assertNotIn("login_url", result)  # create return carries no handoff yet
		self.assertEqual(result["status"], "Pending")

	def test_bad_label_is_rejected(self) -> None:
		"""The pilot's before_insert validates the label — a dotted/uppercase one fails
		loud at create, not at deploy."""
		with self.assertRaises(frappe.ValidationError):
			self._create("Not.A.Label")


class TestRegenerateVMLogin(IntegrationTestCase):
	"""Central's Open path re-mints an expired bench/site login by VM name. The login
	URL lives on the front door (Pilot OR Site — the VM is a pure microVM), so the
	endpoint resolves the VM to its front door, re-mints, and returns the VM-shaped
	Asset payload — a plain VM with no front door is refused."""

	def test_remints_via_front_door_and_returns_vm_payload(self) -> None:
		"""A VM backed by a front door (Pilot or Site): re-mint via it, then return the
		freshly re-read VM-shaped Asset payload (NOT the front door's own return shape —
		a Site's regenerate returns a site-shaped mirror, so we re-derive the VM shape)."""
		front_door = MagicMock()
		vm_payload = {"name": "vm-1", "login_url": "https://acme.blr1.frappe.dev/app?sid=fresh"}
		with (
			patch("atlas.atlas.front_door.front_door_for_vm", return_value=front_door) as resolve,
			patch("atlas.atlas.central_report._vm_payload", return_value=vm_payload),
			patch.object(provision_api.frappe, "get_doc", return_value=MagicMock(name="vm-1")),
		):
			result = provision_api.regenerate_vm_login("vm-1")
		resolve.assert_called_once_with("vm-1")
		front_door.regenerate_login_url.assert_called_once_with()
		self.assertEqual(result, vm_payload)

	def test_vm_without_front_door_is_refused(self) -> None:
		"""A plain proxy/operator VM has no bench/site login to regenerate."""
		with patch("atlas.atlas.front_door.front_door_for_vm", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				provision_api.regenerate_vm_login("vm-plain")
