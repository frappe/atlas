"""Tests for the demo populate script.

The full `demo.run()` commits many times (it drives real controllers), so it is
exercised by hand, not here — running it inside the test transaction would break
isolation. These tests cover the cheap, important invariants: the developer_mode
gate and the static dataset's internal consistency.
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import demo, demo_data
from atlas.atlas.sizes import SIZE_PRESETS


class TestDemoDeveloperModeGate(IntegrationTestCase):
	def setUp(self) -> None:
		self._developer_mode = frappe.local.conf.developer_mode

	def tearDown(self) -> None:
		frappe.local.conf.developer_mode = self._developer_mode

	def test_run_throws_when_developer_mode_off(self) -> None:
		frappe.local.conf.developer_mode = 0
		with self.assertRaises(frappe.ValidationError):
			demo.run()


class TestDemoDatasetConsistency(IntegrationTestCase):
	"""The static tables must reference only keys that exist — a typo here would
	blow up mid-run with a confusing KeyError."""

	def test_vm_presets_are_real(self) -> None:
		for spec in demo_data.VIRTUAL_MACHINES:
			self.assertIn(spec["preset"], SIZE_PRESETS)

	def test_vm_server_keys_exist(self) -> None:
		server_keys = set(demo_data.SERVERS) | {"metal-01"}
		for spec in demo_data.VIRTUAL_MACHINES:
			self.assertIn(spec["server"], server_keys, f"{spec['key']} -> unknown server {spec['server']}")

	def test_vm_image_keys_exist(self) -> None:
		image_keys = set(demo_data.IMAGES)
		for spec in demo_data.VIRTUAL_MACHINES:
			self.assertIn(spec["image"], image_keys, f"{spec['key']} -> unknown image {spec['image']}")

	def test_vm_end_states_are_valid(self) -> None:
		valid = {"Pending", "Running", "Stopped", "Paused", "Terminated", "Failed"}
		for spec in demo_data.VIRTUAL_MACHINES:
			self.assertIn(spec["end"], valid)

	def test_default_user_image_is_one_of_the_images(self) -> None:
		image_names = {fields["image_name"] for fields in demo_data.IMAGES.values()}
		self.assertIn(demo_data.DEFAULT_USER_IMAGE, image_names)

	def test_dataset_spans_every_vm_status(self) -> None:
		# The whole point of the demo is variety — assert every status is present.
		ends = {spec["end"] for spec in demo_data.VIRTUAL_MACHINES}
		self.assertEqual(ends, {"Running", "Stopped", "Paused", "Terminated", "Failed"})

	def test_dataset_spans_every_server_status(self) -> None:
		statuses = {status for _, status in demo_data.SERVERS.values()}
		self.assertSetEqual(statuses, {"Active", "Bootstrapping", "Broken", "Draining"})
