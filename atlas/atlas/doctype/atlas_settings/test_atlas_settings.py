"""Tests for the Atlas Settings Single and its accessor module."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import atlas_settings
from atlas.atlas.providers.base import SshKey
from atlas.tests.fixtures import (
	_ensure_fake_ssh_key_path,
	make_provider_row,
	set_atlas_settings,
)


class TestAtlasSettingsAccessors(IntegrationTestCase):
	def test_get_ssh_key_returns_dataclass(self) -> None:
		provider = make_provider_row(name="test-ssh-prov")
		set_atlas_settings(
			provider,
			ssh_fingerprint="fp:test",
			ssh_public_key="ssh-ed25519 AAAA",
			ssh_private_key_path=_ensure_fake_ssh_key_path(),
		)
		key = atlas_settings.get_ssh_key()
		self.assertIsInstance(key, SshKey)
		self.assertEqual(key.vendor_id, "fp:test")
		self.assertEqual(key.public_key, "ssh-ed25519 AAAA")

	def test_get_ssh_private_key_path_returns_path(self) -> None:
		provider = make_provider_row(name="test-pk-prov")
		set_atlas_settings(provider, ssh_private_key_path=_ensure_fake_ssh_key_path())
		path = atlas_settings.get_ssh_private_key_path()
		self.assertEqual(path, _ensure_fake_ssh_key_path())

	def test_get_provider_throws_when_unset(self) -> None:
		# Save the current value, clear, restore in tearDown via a context.
		previous = frappe.db.get_single_value("Atlas Settings", "provider")
		try:
			frappe.db.set_single_value(
				"Atlas Settings", "provider", "", update_modified=False
			)
			with self.assertRaises(frappe.ValidationError) as raised:
				atlas_settings.get_provider()
			self.assertIn("no active provider", str(raised.exception))
		finally:
			if previous:
				frappe.db.set_single_value(
					"Atlas Settings", "provider", previous, update_modified=False
				)
