"""Unit tests for provisioning helpers.

Covers `region_server_title` / `provision_region`: the per-bench region label
(multiple developers share one DigitalOcean / Scaleway account) that prefixes a
provisioned `Server.title` so each bench's boxes are recognizable in the vendor
console. The region is the single `Atlas Settings.region` source of truth
(`placement.atlas_region`), available from the first bootstrap step.
"""

from __future__ import annotations

import re
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from atlas.atlas.provisioning import provision_region, region_server_title


class TestRegionServerTitle(FrappeTestCase):
	def test_region_reads_atlas_settings(self):
		with patch("atlas.atlas.placement.atlas_region", return_value="blr1"):
			self.assertEqual(provision_region(), "blr1")

	def test_region_fails_loud_when_unset(self):
		import frappe

		with patch(
			"atlas.atlas.placement.atlas_region",
			side_effect=frappe.ValidationError("Set Atlas Settings.region"),
		):
			with self.assertRaises(frappe.ValidationError):
				provision_region()

	def test_title_without_role_is_x_region_hex(self):
		with patch("atlas.atlas.placement.atlas_region", return_value="blr1"):
			title = region_server_title()
		self.assertRegex(title, r"^x-blr1-[0-9a-f]{6}$")

	def test_title_with_role_includes_role(self):
		with patch("atlas.atlas.placement.atlas_region", return_value="blr1"):
			title = region_server_title("e2e")
		self.assertRegex(title, r"^x-blr1-e2e-[0-9a-f]{6}$")

	def test_titles_are_unique_across_calls(self):
		with patch("atlas.atlas.placement.atlas_region", return_value="blr1"):
			titles = {region_server_title() for _ in range(50)}
		self.assertEqual(len(titles), 50)
		self.assertTrue(all(re.fullmatch(r"x-blr1-[0-9a-f]{6}", title) for title in titles))
