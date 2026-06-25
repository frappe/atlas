"""Unit tests for the Scaleway path of `atlas.bootstrap.ensure_provider`.

The end-to-end bootstrap is host-bound (it provisions real infra — that is the
`scaleway_provisioning` e2e's job). What IS unit-answerable in milliseconds is
the *seeding* logic `ensure_provider` does before any provision: that a
`Scaleway` provider type sets `Atlas Settings.provider_type`, seeds `Scaleway
Settings`, runs the load-bearing catalog discover, wires the named default
size/image, and fails loud when a named default isn't in the discovered catalog.
We mock `discover()` (the only network call) so the test is hermetic, exactly as
`test_scaleway.py` mocks the client.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas import bootstrap
from atlas.atlas.providers.base import Capabilities, ImageInfo, SizeInfo

# A REAL readable key file. `Atlas Settings.setup` (the contract ensure_provider now
# drives) does an isfile() check on the key path — matching restore_credentials — so a
# character device like /dev/null no longer passes; spill a regular tempfile instead.
_KEY_PATH = os.path.join(tempfile.gettempdir(), "atlas-bootstrap-test-key.pem")
if not os.path.isfile(_KEY_PATH):
	with open(_KEY_PATH, "w") as _handle:
		_handle.write("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n")
	os.chmod(_KEY_PATH, 0o600)

# A canned two-row catalog the mocked discover() returns — the slugs the config
# below names as defaults plus one extra, so we also prove the wider catalog is
# upserted (not just the named defaults).
SIZE_SLUG = "EM-A610R-NVME"
IMAGE_SLUG = "Ubuntu_24.04"
CAPABILITIES = Capabilities(
	sizes=(
		SizeInfo(slug=SIZE_SLUG, monthly_cost_usd=40, provider_metadata={"offer_id": "offer-uuid"}),
		SizeInfo(slug="EM-B112X-SSD", monthly_cost_usd=120, provider_metadata={"offer_id": "offer-uuid-2"}),
	),
	images=(ImageInfo(slug=IMAGE_SLUG, provider_metadata={"os_id": "os-uuid"}),),
)

SCALEWAY_CONFIG = {
	"atlas_provider_type": "Scaleway",
	"atlas_ssh_private_key_path": _KEY_PATH,  # a real readable file (setup isfile-checks it)
	"atlas_scw_secret_key": "scw-secret-key",
	"atlas_scw_project_id": "proj-uuid",
	"atlas_scw_zone": "fr-par-2",
	"atlas_scw_default_size": SIZE_SLUG,
	"atlas_scw_default_image": IMAGE_SLUG,
	"atlas_scw_billing": "monthly",
}


class _FakeScalewayProvider:
	"""Stand-in for `ScalewayProvider` whose `discover()` returns CAPABILITIES
	with no API call and no `get_secret`/Settings read at construction."""

	def __init__(self) -> None:
		pass

	def discover(self) -> Capabilities:
		return CAPABILITIES


def _patch_conf(overrides: dict):
	"""Patch `frappe.conf` (== `frappe.local.conf`, a _dict) with `overrides`."""
	return patch.dict(frappe.local.conf, overrides)


class TestEnsureProviderScaleway(IntegrationTestCase):
	def setUp(self) -> None:
		# Start from a clean slate: drop any catalog rows a prior run left, so each
		# test seeds from zero. The shared dev DB is also the test DB, so capture +
		# restore the Singles we write (Atlas Settings.provider_type + Scaleway
		# Settings) — a committed fake left behind would break a later real provision,
		# the same trap restore_credentials guards against.
		_cleanup()
		self.addCleanup(_cleanup)
		self.addCleanup(_restore_singles, _snapshot_singles())
		# Avoid the ssh-keygen derivation hitting /dev/null — pin a public key.
		self._public_key_patch = patch.object(
			bootstrap, "_resolve_fleet_public_key", return_value="ssh-ed25519 AAAA bootstrap"
		)
		self._public_key_patch.start()
		self.addCleanup(self._public_key_patch.stop)
		self._provider_patch = patch("atlas.atlas.providers.scaleway.ScalewayProvider", _FakeScalewayProvider)
		self._provider_patch.start()
		self.addCleanup(self._provider_patch.stop)

	def test_seeds_provider_settings_and_catalog(self) -> None:
		with _patch_conf(SCALEWAY_CONFIG):
			provider_type = bootstrap.ensure_provider()

		# ensure_provider returns the Scaleway type string and records it on Atlas Settings.
		self.assertEqual(provider_type, "Scaleway")
		self.assertEqual(frappe.db.get_single_value("Atlas Settings", "provider_type"), "Scaleway")

		# Scaleway Settings seeded from config (the secret via the password store).
		self.assertEqual(frappe.db.get_single_value("Scaleway Settings", "zone"), "fr-par-2")
		self.assertEqual(frappe.db.get_single_value("Scaleway Settings", "project_id"), "proj-uuid")
		self.assertEqual(frappe.db.get_single_value("Scaleway Settings", "billing"), "monthly")
		from atlas.atlas.secrets import get_secret

		self.assertEqual(get_secret("Scaleway Settings", "Scaleway Settings", "secret_key"), "scw-secret-key")

		# Discover upserted the WHOLE catalog (both sizes), with the per-zone UUIDs
		# the provision() resolver reads back out of provider_metadata.
		import json

		self.assertTrue(frappe.db.exists("Provider Size", f"Scaleway/{SIZE_SLUG}"))
		self.assertTrue(frappe.db.exists("Provider Size", "Scaleway/EM-B112X-SSD"))
		self.assertTrue(frappe.db.exists("Provider Image", f"Scaleway/{IMAGE_SLUG}"))
		metadata = json.loads(
			frappe.db.get_value("Provider Size", f"Scaleway/{SIZE_SLUG}", "provider_metadata")
		)
		self.assertEqual(metadata["offer_id"], "offer-uuid")

		# Defaults on Scaleway Settings point at the named rows (prefixed).
		self.assertEqual(
			frappe.db.get_single_value("Scaleway Settings", "default_size"), f"Scaleway/{SIZE_SLUG}"
		)
		self.assertEqual(
			frappe.db.get_single_value("Scaleway Settings", "default_image"), f"Scaleway/{IMAGE_SLUG}"
		)

	def test_unknown_default_size_throws(self) -> None:
		config = {**SCALEWAY_CONFIG, "atlas_scw_default_size": "EM-TYPO-NVME"}
		with _patch_conf(config), self.assertRaises(frappe.ValidationError) as caught:
			bootstrap.ensure_provider()
		self.assertIn("EM-TYPO-NVME", str(caught.exception))

	def test_billing_defaults_to_hourly_when_unset(self) -> None:
		config = {key: value for key, value in SCALEWAY_CONFIG.items() if key != "atlas_scw_billing"}
		with _patch_conf(config):
			bootstrap.ensure_provider()
		self.assertEqual(frappe.db.get_single_value("Scaleway Settings", "billing"), "hourly")


class TestEnsureProviderValidation(IntegrationTestCase):
	def test_rejects_unknown_provider_type(self) -> None:
		with (
			_patch_conf({"atlas_provider_type": "Linode"}),
			self.assertRaises(frappe.ValidationError) as caught,
		):
			bootstrap.ensure_provider()
		self.assertIn("DigitalOcean, Scaleway or Self-Managed", str(caught.exception))


_TOUCHED_SINGLES = (
	("Atlas Settings", "provider_type"),
	("Atlas Settings", "region"),
	("Atlas Settings", "ssh_private_key_path"),
	("Atlas Settings", "ssh_public_key"),
	("Scaleway Settings", "zone"),
	("Scaleway Settings", "project_id"),
	("Scaleway Settings", "organization_id"),
	("Scaleway Settings", "billing"),
	("Scaleway Settings", "default_size"),
	("Scaleway Settings", "default_image"),
)


def _snapshot_singles() -> dict:
	return {(dt, field): frappe.db.get_single_value(dt, field) for dt, field in _TOUCHED_SINGLES}


def _restore_singles(snapshot: dict) -> None:
	for (dt, field), value in snapshot.items():
		frappe.db.set_single_value(dt, field, value, update_modified=False)
	frappe.db.commit()


def _cleanup() -> None:
	for size in (SIZE_SLUG, "EM-B112X-SSD", "EM-TYPO-NVME"):
		name = f"Scaleway/{size}"
		if frappe.db.exists("Provider Size", name):
			frappe.delete_doc("Provider Size", name, force=True, ignore_permissions=True)
	image_name = f"Scaleway/{IMAGE_SLUG}"
	if frappe.db.exists("Provider Image", image_name):
		frappe.delete_doc("Provider Image", image_name, force=True, ignore_permissions=True)
	frappe.db.commit()
