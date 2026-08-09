"""The size-preset ladder is one source of truth (atlas/atlas/sizes.py).

These tests pin that the five tiers (Shared 1x/2x/4x/8x, Dedicated 1x) are
internally consistent and that the Virtual Machine `size_preset` Select options
in the doctype JSON are regenerated from `sizes.size_preset_options()` — so the
JSON and the canonical dict can't drift. The desk `.js` carries matching
literals (JS can't import the Python source); it is documented as such and
checked by eye against this module.
"""

import json
import math
import pathlib

from frappe.tests import IntegrationTestCase

from atlas.atlas import sizes

_VM_JSON = (
	pathlib.Path(__file__).resolve().parents[1]
	/ "atlas"
	/ "doctype"
	/ "virtual_machine"
	/ "virtual_machine.json"
)


class TestSizes(IntegrationTestCase):
	def test_five_tiers(self) -> None:
		self.assertEqual(len(sizes.SIZE_PRESETS), 5)
		self.assertEqual(
			list(sizes.SIZE_PRESETS),
			["Shared 1x", "Shared 2x", "Shared 4x", "Shared 8x", "Dedicated 1x"],
		)

	def test_cpu_bandwidth_ladder(self) -> None:
		# Shared 1x is the base unit (1/16 core), 2x/4x/8x double it, Dedicated 1x
		# is a full core.
		expected = [0.0625, 0.125, 0.25, 0.5, 1]
		actual = [p["cpu_max_cores"] for p in sizes.SIZE_PRESETS.values()]
		self.assertEqual(actual, expected)

	def test_vcpus_is_ceil_of_bandwidth_min_one(self) -> None:
		# vcpus (the guest vcpu_count) is the whole-thread count the guest boots:
		# ceil(cpu_max_cores), at least 1. Sub-1 caps still boot one thread.
		for label, p in sizes.SIZE_PRESETS.items():
			expected = max(1, math.ceil(p["cpu_max_cores"]))
			self.assertEqual(p["vcpus"], expected, label)

	def test_memory_and_disk_present(self) -> None:
		for label, p in sizes.SIZE_PRESETS.items():
			self.assertGreater(p["memory_megabytes"], 0, label)
			self.assertGreater(p["disk_gigabytes"], 0, label)

	def test_share_unit_is_shared_1x(self) -> None:
		# The share unit is derived from the ladder itself — Shared 1x's cost on the
		# three packed axes — so there is exactly one source of truth.
		self.assertEqual(sizes.SHARE_UNIT["cpu_max_cores"], 0.0625)
		self.assertEqual(sizes.SHARE_UNIT["memory_megabytes"], 512)
		self.assertEqual(sizes.SHARE_UNIT["disk_gigabytes"], 10)

	def test_proportionality_invariant(self) -> None:
		# THE load-bearing property: every preset is the SAME integer multiple of the
		# share unit on all three axes. This is what makes packing one-dimensional and
		# even-spread free (the placement scorer's relative-fill spread relies on it —
		# spec/28). Break it deliberately (a non-proportional plan) and you must
		# revisit spec/28 and the scorer, not just this test.
		unit = sizes.SHARE_UNIT
		for label, preset in sizes.SIZE_PRESETS.items():
			cpu_units = preset["cpu_max_cores"] / unit["cpu_max_cores"]
			memory_units = preset["memory_megabytes"] / unit["memory_megabytes"]
			disk_units = preset["disk_gigabytes"] / unit["disk_gigabytes"]
			self.assertEqual(
				cpu_units,
				int(cpu_units),
				f"{label}: cpu is not a whole number of share units — packing is no "
				f"longer one-dimensional; revisit spec/28 and the placement scorer",
			)
			self.assertEqual(
				(cpu_units, memory_units, disk_units),
				(int(cpu_units), int(cpu_units), int(cpu_units)),
				f"{label}: axes are not the same multiple of the share unit "
				f"({cpu_units}, {memory_units}, {disk_units}) — the scorer's "
				f"even-spread-is-free property assumes proportional presets; revisit spec/28",
			)

	def test_options_string_starts_with_custom(self) -> None:
		options = sizes.size_preset_options().split("\n")
		self.assertEqual(options[0], "Custom")
		self.assertEqual(options[1:], list(sizes.SIZE_PRESETS.keys()))

	def test_doctype_json_select_matches_canonical(self) -> None:
		# The Virtual Machine.size_preset Select options must equal what
		# sizes.size_preset_options() emits — regenerate the JSON from the dict if
		# this fails, don't hand-edit one side.
		schema = json.loads(_VM_JSON.read_text())
		field = next(f for f in schema["fields"] if f["fieldname"] == "size_preset")
		self.assertEqual(field["options"], sizes.size_preset_options())
