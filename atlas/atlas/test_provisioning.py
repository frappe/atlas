"""Unit tests for provisioning helpers.

Covers `region_server_title` / `provision_region`: the per-bench region label
(multiple developers share one DigitalOcean / Scaleway account) that prefixes a
provisioned `Server.title` so each bench's boxes are recognizable in the vendor
console. The region is resolved without a live Root Domain (it must work from the
first bootstrap step), so these are pure config tests.
"""

from __future__ import annotations

import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from atlas.atlas.provisioning import provision_region, region_server_title


class TestRegionServerTitle(FrappeTestCase):
	def test_region_prefers_tls_region_conf(self):
		with patch.dict(frappe.conf, {"atlas_tls_region": "blr1", "atlas_do_region": "nyc3"}):
			self.assertEqual(provision_region(), "blr1")

	def test_region_falls_back_to_do_region_conf(self):
		conf = {key: frappe.conf.get(key) for key in ("atlas_tls_region", "atlas_do_region")}
		try:
			frappe.conf["atlas_do_region"] = "nyc3"
			frappe.conf.pop("atlas_tls_region", None)
			self.assertEqual(provision_region(), "nyc3")
		finally:
			frappe.conf.update({key: value for key, value in conf.items() if value is not None})

	def test_region_falls_back_to_active_root_domain(self):
		conf = {key: frappe.conf.get(key) for key in ("atlas_tls_region", "atlas_do_region")}
		try:
			frappe.conf.pop("atlas_tls_region", None)
			frappe.conf.pop("atlas_do_region", None)
			with patch(
				"atlas.atlas.placement.active_root_domain",
				return_value=frappe._dict(region="zone9"),
			):
				self.assertEqual(provision_region(), "zone9")
		finally:
			frappe.conf.update({key: value for key, value in conf.items() if value is not None})

	def test_region_defaults_to_x_when_unconfigured(self):
		conf = {key: frappe.conf.get(key) for key in ("atlas_tls_region", "atlas_do_region")}
		try:
			frappe.conf.pop("atlas_tls_region", None)
			frappe.conf.pop("atlas_do_region", None)
			with patch(
				"atlas.atlas.placement.active_root_domain",
				side_effect=frappe.ValidationError("No domain is configured"),
			):
				self.assertEqual(provision_region(), "x")
		finally:
			frappe.conf.update({key: value for key, value in conf.items() if value is not None})

	def test_title_without_role_is_x_region_hex(self):
		with patch.dict(frappe.conf, {"atlas_tls_region": "blr1"}):
			title = region_server_title()
		self.assertRegex(title, r"^x-blr1-[0-9a-f]{6}$")

	def test_title_with_role_includes_role(self):
		with patch.dict(frappe.conf, {"atlas_tls_region": "blr1"}):
			title = region_server_title("e2e")
		self.assertRegex(title, r"^x-blr1-e2e-[0-9a-f]{6}$")

	def test_titles_are_unique_across_calls(self):
		with patch.dict(frappe.conf, {"atlas_tls_region": "blr1"}):
			titles = {region_server_title() for _ in range(50)}
		self.assertEqual(len(titles), 50)
		self.assertTrue(all(re.fullmatch(r"x-blr1-[0-9a-f]{6}", title) for title in titles))
