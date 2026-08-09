import json

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api.inventory import available_frappe_versions
from atlas.atlas.placement import image_for_version, version_from_image, version_image_map
from atlas.tests.fixtures import make_image, no_commit_enqueue


class TestFrappeVersionImage(IntegrationTestCase):
	"""The version↔image bridge: Central picks a Frappe version, Atlas resolves it to the
	`bench-<token>-admin` Pilot image and reports the token back so the mirror is ground
	truth. A Central "server" is a Pilot (admin console), so the version maps to the admin
	variant — the plain `bench-<token>` image backs a self-serve Site instead (spec/14)."""

	def test_version_from_image_parses_bench_names(self):
		self.assertEqual(version_from_image("bench-v16"), "v16")
		self.assertEqual(version_from_image("bench-nightly"), "nightly")
		# The -admin variant reports the same version token (never surfaced separately).
		self.assertEqual(version_from_image("bench-v16-admin"), "v16")
		# A non-bench / plain image carries no version.
		self.assertIsNone(version_from_image("ubuntu-24.04"))
		self.assertIsNone(version_from_image(None))

	def test_image_for_version_resolves_active_admin_image(self):
		# Central's create_vm stands up a Pilot (admin console), so a version resolves to
		# its `-admin` variant — not the plain `bench-v16` site image.
		with no_commit_enqueue():
			make_image("bench-v16-admin", is_active=1)
		self.assertEqual(image_for_version("v16"), "bench-v16-admin")

	def test_image_for_version_falls_back_when_unbuilt(self):
		# A version with no active image must not block the create — it resolves to the
		# operator default instead. Pin one so the fallback is deterministic here.
		with no_commit_enqueue():
			make_image("only-default", is_active=1)
		frappe.db.set_single_value("Atlas Settings", "default_user_image", "only-default")
		self.assertEqual(image_for_version("v99-unbuilt"), "only-default")
		self.assertEqual(image_for_version(None), "only-default")

	def test_available_versions_lists_admin_bench_images_only(self):
		# Central offers Pilots (admin consoles), so its version picker is drawn from the
		# active `-admin` images — the plain site image and non-bench images are excluded.
		with no_commit_enqueue():
			make_image("bench-v15-admin", is_active=1)
			make_image("bench-v16", is_active=1)  # plain site variant — not offered
			make_image("plain-os", is_active=1)
		versions = available_frappe_versions()
		self.assertIn("v15", versions)  # from bench-v15-admin
		self.assertNotIn("v16", versions)  # plain bench-v16 (site image) excluded
		self.assertNotIn("plain-os", versions)  # non-bench image excluded

	def test_version_image_map_pairs_each_version_with_its_admin_image(self):
		# The operator-visible map: same admin-image source as available_frappe_versions,
		# but carrying what each version resolves to. Every key resolves to its value via
		# image_for_version, so the visible map can't drift from the provisioning path.
		# Unique tokens so a sibling test's leftover images can't perturb the assertions.
		with no_commit_enqueue():
			make_image("bench-map15-admin", is_active=1)
			make_image("bench-map16", is_active=1)  # plain site variant — excluded
			make_image("plain-map-os", is_active=1)  # non-bench — excluded
		mapping = version_image_map()
		self.assertEqual(mapping.get("map15"), "bench-map15-admin")
		self.assertNotIn("map16", mapping)  # plain site variant carries no admin image
		self.assertNotIn("plain-map-os", mapping)
		# Keys match the offered versions; each resolves to its mapped image.
		self.assertEqual(set(mapping), set(available_frappe_versions()))
		for version, image in mapping.items():
			self.assertEqual(image_for_version(version), image)

	def test_central_settings_onload_computes_the_map(self):
		# The form shows the map live on open — read-only, computed by onload, never stored.
		with no_commit_enqueue():
			make_image("bench-v16-admin", is_active=1)
		settings = frappe.get_cached_doc("Central Settings")
		settings.run_method("onload")
		self.assertEqual(json.loads(settings.version_image_map).get("v16"), "bench-v16-admin")
