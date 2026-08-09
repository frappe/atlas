"""Unit tests for the `is_default` invariant on Provider Size (and, by shared
implementation, Provider Image): at most one row per provider_type is the default,
and setting a new one flips the previous off in the same save. Also covers the
catalog helpers in `setup_catalog` and the discover()-hint precedence in
`upsert_catalog` (hint fills only an empty slot; an explicit choice always wins).
"""

from __future__ import annotations

import json

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.providers.base import Capabilities, ImageInfo, SizeInfo
from atlas.atlas.provisioning import upsert_catalog
from atlas.atlas.setup_catalog import clear_other_defaults, default_name, set_default

_PT = "Fake"  # isolate from DigitalOcean/Scaleway rows other tests seed


def _make_size(slug: str, is_default: bool = False) -> None:
	frappe.get_doc(
		{
			"doctype": "Provider Size",
			"provider_type": _PT,
			"slug": slug,
			"enabled": 1,
			"is_default": int(is_default),
			"provider_metadata": json.dumps({}),
		}
	).insert(ignore_permissions=True)


class TestProviderSizeDefaultInvariant(IntegrationTestCase):
	def setUp(self) -> None:
		self.addCleanup(self._cleanup)

	def _cleanup(self) -> None:
		for name in frappe.get_all("Provider Size", filters={"provider_type": _PT}, pluck="name"):
			frappe.delete_doc("Provider Size", name, force=True, ignore_permissions=True)
		for name in frappe.get_all("Provider Image", filters={"provider_type": _PT}, pluck="name"):
			frappe.delete_doc("Provider Image", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_marking_a_default_unsets_the_previous(self) -> None:
		_make_size("size-a", is_default=True)
		_make_size("size-b")
		# Flipping size-b default must flip size-a off (one default per provider_type).
		set_default("Provider Size", _PT, "size-b")
		self.assertEqual(frappe.db.get_value("Provider Size", f"{_PT}/size-a", "is_default"), 0)
		self.assertEqual(frappe.db.get_value("Provider Size", f"{_PT}/size-b", "is_default"), 1)
		self.assertEqual(default_name("Provider Size", _PT), f"{_PT}/size-b")

	def test_default_name_empty_when_none_marked(self) -> None:
		_make_size("size-a")
		self.assertEqual(default_name("Provider Size", _PT), "")

	def test_clear_other_defaults_keeps_named_row(self) -> None:
		_make_size("size-a", is_default=True)
		_make_size("size-b", is_default=True)  # bypasses the controller's own clear
		clear_other_defaults("Provider Size", _PT, keep=f"{_PT}/size-b")
		self.assertEqual(frappe.db.get_value("Provider Size", f"{_PT}/size-a", "is_default"), 0)
		self.assertEqual(frappe.db.get_value("Provider Size", f"{_PT}/size-b", "is_default"), 1)


class TestUpsertCatalogDefaultHint(IntegrationTestCase):
	def setUp(self) -> None:
		self.addCleanup(self._cleanup)

	def _cleanup(self) -> None:
		for name in frappe.get_all("Provider Size", filters={"provider_type": _PT}, pluck="name"):
			frappe.delete_doc("Provider Size", name, force=True, ignore_permissions=True)
		for name in frappe.get_all("Provider Image", filters={"provider_type": _PT}, pluck="name"):
			frappe.delete_doc("Provider Image", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _caps(self, default_slug: str) -> Capabilities:
		return Capabilities(
			sizes=(
				SizeInfo(slug="cheap", monthly_cost_usd=5, is_default=default_slug == "cheap"),
				SizeInfo(slug="big", monthly_cost_usd=50, is_default=default_slug == "big"),
			),
			images=(ImageInfo(slug="os", is_default=True),),
		)

	def test_hint_fills_empty_slot(self) -> None:
		upsert_catalog(_PT, self._caps("cheap"))
		self.assertEqual(default_name("Provider Size", _PT), f"{_PT}/cheap")
		self.assertEqual(default_name("Provider Image", _PT), f"{_PT}/os")

	def test_hint_does_not_override_existing_default(self) -> None:
		# Operator already chose 'big'; a re-discover hinting 'cheap' must not move it.
		upsert_catalog(_PT, self._caps("cheap"))
		set_default("Provider Size", _PT, "big")
		upsert_catalog(_PT, self._caps("cheap"))
		self.assertEqual(default_name("Provider Size", _PT), f"{_PT}/big")
