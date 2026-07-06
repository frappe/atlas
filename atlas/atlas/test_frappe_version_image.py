import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api.inventory import available_frappe_versions
from atlas.atlas.placement import image_for_version, version_from_image
from atlas.tests.fixtures import make_image, no_commit_enqueue


class TestFrappeVersionImage(IntegrationTestCase):
	"""The version↔image bridge: Central picks a Frappe version, Atlas resolves it to
	a `bench-<token>` image and reports the token back so the mirror is ground truth."""

	def test_version_from_image_parses_bench_names(self):
		self.assertEqual(version_from_image("bench-v16"), "v16")
		self.assertEqual(version_from_image("bench-nightly"), "nightly")
		# The -admin variant reports the same version token (never surfaced separately).
		self.assertEqual(version_from_image("bench-v16-admin"), "v16")
		# A non-bench / plain image carries no version.
		self.assertIsNone(version_from_image("ubuntu-24.04"))
		self.assertIsNone(version_from_image(None))

	def test_image_for_version_resolves_active_bench_image(self):
		with no_commit_enqueue():
			make_image("bench-v16", is_active=1)
		self.assertEqual(image_for_version("v16"), "bench-v16")

	def test_image_for_version_falls_back_when_unbuilt(self):
		# A version with no active image must not block the create — it resolves to the
		# operator default instead. Pin one so the fallback is deterministic here.
		with no_commit_enqueue():
			make_image("only-default", is_active=1)
		frappe.db.set_single_value("Atlas Settings", "default_user_image", "only-default")
		self.assertEqual(image_for_version("v99-unbuilt"), "only-default")
		self.assertEqual(image_for_version(None), "only-default")

	def test_available_versions_lists_plain_bench_images_only(self):
		with no_commit_enqueue():
			make_image("bench-v15", is_active=1)
			make_image("bench-v15-admin", is_active=1)
			make_image("plain-os", is_active=1)
		versions = available_frappe_versions()
		self.assertIn("v15", versions)
		self.assertNotIn("v15-admin", versions)  # -admin variant excluded
		self.assertNotIn("plain-os", versions)  # non-bench image excluded
