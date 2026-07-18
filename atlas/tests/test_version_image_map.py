"""Central resolves a picked Frappe version (v16 / nightly) through the admin bench
image map — `version_image_map` / `image_for_version` in atlas/atlas/placement.py.

The wrinkle these tests pin: a rebaked image can't replace the old one in place
(the old image's snapshots keep it undeletable), so it ships under a new name with a
trailing generation — `bench-v16-1-admin` alongside `bench-v16-admin`. Central still
offers and looks up the clean `v16`, so the token strips the generation and the newest
active image wins the key. The value stays the exact image name to provision from.
"""

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.placement import image_for_version, version_from_image, version_image_map
from atlas.tests.fixtures import make_image


class TestVersionImageMap(IntegrationTestCase):
	def setUp(self) -> None:
		self.addCleanup(frappe.set_user, "Administrator")
		# The map reads every active admin image fleet-wide, so neutralize any left by
		# other suites / fixtures — this test's own images must be the only entries.
		for name in frappe.get_all("Virtual Machine Image", filters={"is_active": 1}, pluck="name"):
			frappe.db.set_value("Virtual Machine Image", name, "is_active", 0)

	def test_token_strips_prefix_and_admin_suffix(self) -> None:
		self.assertEqual(version_from_image("bench-v16-admin"), "v16")
		self.assertEqual(version_from_image("bench-nightly-admin"), "nightly")

	def test_token_strips_rebake_generation(self) -> None:
		self.assertEqual(version_from_image("bench-v16-1-admin"), "v16")
		self.assertEqual(version_from_image("bench-nightly-2-admin"), "nightly")

	def test_token_none_for_non_bench_image(self) -> None:
		self.assertIsNone(version_from_image("proxy"))
		self.assertIsNone(version_from_image(None))

	def test_map_keys_are_clean_versions(self) -> None:
		make_image("bench-v16-admin")
		make_image("bench-nightly-admin")
		self.assertEqual(
			version_image_map(),
			{"v16": "bench-v16-admin", "nightly": "bench-nightly-admin"},
		)

	def test_rebake_generation_wins_the_key(self) -> None:
		# Old and new both active — old undeletable while snapshots pin it. Insert
		# oldest first (fixtures insert in call order, so creation asc is that order);
		# the newer generation must own the `v16` key and be what provisioning picks.
		make_image("bench-v16-admin")
		make_image("bench-v16-1-admin")
		self.assertEqual(version_image_map(), {"v16": "bench-v16-1-admin"})
		self.assertEqual(image_for_version("v16"), "bench-v16-1-admin")

	def test_image_for_version_falls_back_when_version_unbuilt(self) -> None:
		# No admin image for the token → resolve the configured default rather than
		# blocking provisioning. A single active image makes default_image() unambiguous.
		make_image("some-default-image")
		frappe.db.set_single_value("Atlas Settings", "default_user_image", "some-default-image")
		self.addCleanup(frappe.db.set_single_value, "Atlas Settings", "default_user_image", None)
		self.assertEqual(image_for_version("v99"), "some-default-image")
