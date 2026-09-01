from __future__ import annotations

from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.scaleway import ScalewayProvider


class TestScalewayProvider(UnitTestCase):
	def test_accepts_supported_os_version(self) -> None:
		image = ScalewayProvider._accepted_image({"name": "Ubuntu", "version": "24.04 LTS"})

		self.assertIsNotNone(image)
		self.assertEqual(image.os, "Ubuntu")
		self.assertEqual(image.version, "24.04")

	def test_rejects_unsupported_os_version(self) -> None:
		image = ScalewayProvider._accepted_image({"name": "Ubuntu", "version": "13"})

		self.assertIsNone(image)
