"""Unit tests for the explicit setup contract (`atlas/setup.py`) + the Layer-1
`setup()` setters on each Settings Single.

The contract is pure logic — no host, no network except the providers' `discover()`,
which we mock (the only network call, exactly as `test_bootstrap.py` does). We assert
the Singles / catalog / Root Domain a `setup.run(config)` writes match what
`bootstrap.run` wrote before this change, per provider x TLS on/off.

Region distinction (the load-bearing fix): `Atlas Settings.region` is THIS Atlas's
single region (the source of truth); the vendor's OWN region/zone
(`DigitalOcean Settings.region`, `Scaleway Settings.zone`) is independent. The tests
set them to DIFFERENT values and assert both land on the right field.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas import setup
from atlas.atlas.providers.base import Capabilities, ImageInfo, SizeInfo
from atlas.atlas.secrets import get_secret

# A real readable key file so AtlasSettings.setup's file-exists check + ssh-keygen
# derivation have something to chew on (the derivation may fail on a non-key file;
# the setter tolerates that and just leaves ssh_public_key unset).
_KEY_PATH = os.path.join(tempfile.gettempdir(), "atlas-setup-test-key.pem")

DO_CAPS = Capabilities(
	sizes=(SizeInfo(slug="s-2vcpu-4gb-intel", monthly_cost_usd=24, provider_metadata={}),),
	images=(ImageInfo(slug="ubuntu-24-04-x64", provider_metadata={}),),
)
SCW_SIZE = "EM-A610R-NVME"
SCW_IMAGE = "Ubuntu_24.04"
SCW_CAPS = Capabilities(
	sizes=(SizeInfo(slug=SCW_SIZE, monthly_cost_usd=40, provider_metadata={"offer_id": "offer-uuid"}),),
	images=(ImageInfo(slug=SCW_IMAGE, provider_metadata={"os_id": "os-uuid"}),),
)


def _do_config(**over) -> dict:
	config = {
		"provider": {
			"provider_type": "DigitalOcean",
			"region": "blr1",  # Atlas region (source of truth)
			"ssh_private_key_path": _KEY_PATH,
			"ssh_public_key": "ssh-ed25519 AAAA test",
			"digitalocean": {
				"api_token": "dop_v1_test",
				"region": "ams3",  # DO's OWN region — DELIBERATELY different from blr1
				"default_size": "s-2vcpu-4gb-intel",
				"default_image": "ubuntu-24-04-x64",
				"ssh_key_id": "key-id-123",
			},
		}
	}
	config.update(over)
	return config


def _scw_config(**over) -> dict:
	config = {
		"provider": {
			"provider_type": "Scaleway",
			"region": "blr1",  # Atlas region
			"ssh_private_key_path": _KEY_PATH,
			"ssh_public_key": "ssh-ed25519 AAAA test",
			"scaleway": {
				"secret_key": "scw-secret",
				"project_id": "proj-uuid",
				"zone": "fr-par-2",  # Scaleway's OWN zone — different from the Atlas region
				"default_size": SCW_SIZE,
				"default_image": SCW_IMAGE,
				"billing": "monthly",
			},
		}
	}
	config.update(over)
	return config


_TLS_BLOCK = {
	"domain": "blr1.frappe.dev",
	"region": "blr1",
	"access_key_id": "AKIA_TEST",
	"secret_access_key": "route53-secret",
	"aws_region": "eu-west-1",
	"account_email": "ops@example.com",
	"acme_directory_url": "https://acme-staging-v02.api.letsencrypt.org/directory",
}


class _FakeDO:
	def discover(self) -> Capabilities:
		return DO_CAPS


class _FakeSCW:
	def discover(self) -> Capabilities:
		return SCW_CAPS


class TestSetupContract(IntegrationTestCase):
	def setUp(self) -> None:
		if not os.path.isfile(_KEY_PATH):
			with open(_KEY_PATH, "w") as handle:
				handle.write("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n")
			os.chmod(_KEY_PATH, 0o600)
		self.addCleanup(_cleanup)
		self.addCleanup(_restore_singles, _snapshot_singles())
		self._do = patch("atlas.atlas.providers.digitalocean.DigitalOceanProvider", _FakeDO)
		self._scw = patch("atlas.atlas.providers.scaleway.ScalewayProvider", _FakeSCW)
		self._do.start()
		self._scw.start()
		self.addCleanup(self._do.stop)
		self.addCleanup(self._scw.stop)

	# --- region distinction (the fix) -------------------------------------

	def test_digitalocean_region_distinct_from_atlas_region(self) -> None:
		setup.run(_do_config())
		# Atlas Settings.region = this Atlas's single region (source of truth).
		self.assertEqual(frappe.db.get_single_value("Atlas Settings", "region"), "blr1")
		# DigitalOcean Settings.region = DO's OWN API region, independent value.
		self.assertEqual(frappe.db.get_single_value("DigitalOcean Settings", "region"), "ams3")

	def test_scaleway_zone_distinct_from_atlas_region(self) -> None:
		setup.run(_scw_config())
		self.assertEqual(frappe.db.get_single_value("Atlas Settings", "region"), "blr1")
		self.assertEqual(frappe.db.get_single_value("Scaleway Settings", "zone"), "fr-par-2")

	# --- DigitalOcean setter ----------------------------------------------

	def test_digitalocean_setter_writes_fields_and_catalog(self) -> None:
		setup.run(_do_config())
		self.assertEqual(frappe.db.get_single_value("Atlas Settings", "provider_type"), "DigitalOcean")
		self.assertEqual(frappe.db.get_single_value("Atlas Settings", "ssh_private_key_path"), _KEY_PATH)
		self.assertEqual(
			frappe.db.get_single_value("Atlas Settings", "ssh_public_key"), "ssh-ed25519 AAAA test"
		)
		self.assertEqual(frappe.db.get_single_value("DigitalOcean Settings", "ssh_key_id"), "key-id-123")
		self.assertEqual(
			frappe.db.get_single_value("DigitalOcean Settings", "default_size"),
			"DigitalOcean/s-2vcpu-4gb-intel",
		)
		self.assertEqual(
			get_secret("DigitalOcean Settings", "DigitalOcean Settings", "api_token"), "dop_v1_test"
		)
		self.assertTrue(frappe.db.exists("Provider Size", "DigitalOcean/s-2vcpu-4gb-intel"))

	# --- Scaleway setter (load-bearing discover ordering + casing check) ---

	def test_scaleway_setter_seeds_catalog_and_defaults(self) -> None:
		setup.run(_scw_config())
		self.assertEqual(frappe.db.get_single_value("Scaleway Settings", "billing"), "monthly")
		self.assertEqual(get_secret("Scaleway Settings", "Scaleway Settings", "secret_key"), "scw-secret")
		self.assertEqual(
			frappe.db.get_single_value("Scaleway Settings", "default_size"), f"Scaleway/{SCW_SIZE}"
		)
		self.assertTrue(frappe.db.exists("Provider Image", f"Scaleway/{SCW_IMAGE}"))

	def test_scaleway_unknown_default_throws(self) -> None:
		config = _scw_config()
		config["provider"]["scaleway"]["default_size"] = "EM-TYPO-NVME"
		with self.assertRaises(frappe.ValidationError) as caught:
			setup.run(config)
		self.assertIn("EM-TYPO-NVME", str(caught.exception))

	# --- TLS block --------------------------------------------------------

	def test_tls_block_seeds_route53_le_and_root_domain(self) -> None:
		setup.run(_do_config(tls=_TLS_BLOCK))
		self.assertEqual(frappe.db.get_single_value("Route53 Settings", "access_key_id"), "AKIA_TEST")
		self.assertEqual(frappe.db.get_single_value("Route53 Settings", "region"), "eu-west-1")
		self.assertEqual(
			get_secret("Route53 Settings", "Route53 Settings", "secret_access_key"), "route53-secret"
		)
		self.assertEqual(
			frappe.db.get_single_value("Lets Encrypt Settings", "account_email"), "ops@example.com"
		)
		self.assertEqual(frappe.db.get_single_value("Atlas Settings", "dns_provider_type"), "Route53")
		self.assertEqual(frappe.db.get_single_value("Atlas Settings", "tls_provider_type"), "Let's Encrypt")
		self.assertTrue(frappe.db.exists("Root Domain", "blr1.frappe.dev"))

	def test_no_tls_block_skips_tls(self) -> None:
		setup.run(_do_config())
		self.assertFalse(frappe.db.exists("Root Domain", "blr1.frappe.dev"))

	# --- self-managed networking is NOT a Single --------------------------

	def test_self_managed_networking_returned_not_stored(self) -> None:
		config = {
			"provider": {
				"provider_type": "Self-Managed",
				"region": "blr1",
				"ssh_private_key_path": _KEY_PATH,
				"self_managed": {
					"ipv4_address": "1.2.3.4",
					"ipv6_address": "2001:db8::1",
					"ipv6_prefix": "2001:db8::/56",
					"ipv6_virtual_machine_range": "2001:db8:0:1::/64",
				},
			}
		}
		setup.run(config)
		self.assertEqual(frappe.db.get_single_value("Atlas Settings", "provider_type"), "Self-Managed")
		networking = setup.self_managed_networking(config)
		self.assertEqual(networking["ipv4_address"], "1.2.3.4")

	# --- validation -------------------------------------------------------

	def test_missing_provider_block_throws(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			setup.run({})

	def test_rejects_unknown_provider_type(self) -> None:
		config = {
			"provider": {
				"provider_type": "Linode",
				"region": "blr1",
				"ssh_private_key_path": _KEY_PATH,
			}
		}
		with self.assertRaises(frappe.ValidationError):
			setup.run(config)

	def test_missing_key_file_warns_but_persists(self) -> None:
		"""A missing key file must NOT abort setup — it used to throw and roll the
		whole stage back, taking the vendor credentials with it. The file is only
		needed at provision time, so config (incl. the encrypted token) must persist
		and the operator gets a warning."""
		config = _do_config()
		# An explicit public key, since we can't derive one from a non-existent file.
		config["provider"]["ssh_public_key"] = "ssh-ed25519 AAAA explicit"
		config["provider"]["ssh_private_key_path"] = "/no/such/key"
		setup.run(config)  # no raise
		self.assertEqual(frappe.db.get_single_value("Atlas Settings", "provider_type"), "DigitalOcean")
		self.assertEqual(frappe.db.get_single_value("Atlas Settings", "ssh_private_key_path"), "/no/such/key")
		# The credential is the canary: it is written LAST, so it only survives if the
		# missing-file path no longer aborts.
		self.assertEqual(
			get_secret("DigitalOcean Settings", "DigitalOcean Settings", "api_token"), "dop_v1_test"
		)


class TestWizardStages(IntegrationTestCase):
	"""The Setup Wizard front-end: `get_setup_stages(args)` returns the right stages
	and the stage fns apply the setters. Frappe posts slide values as strings — mirror
	that, and assert provider-switch + opt-out behave."""

	def setUp(self) -> None:
		if not os.path.isfile(_KEY_PATH):
			with open(_KEY_PATH, "w") as handle:
				handle.write("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n")
			os.chmod(_KEY_PATH, 0o600)
		self.addCleanup(_cleanup)
		self.addCleanup(_restore_singles, _snapshot_singles())
		self._do = patch("atlas.atlas.providers.digitalocean.DigitalOceanProvider", _FakeDO)
		self._do.start()
		self.addCleanup(self._do.stop)

	def _do_args(self, **over) -> dict:
		args = {
			"provider_type": "DigitalOcean",
			"region": "blr1",
			"ssh_private_key_path": _KEY_PATH,
			"ssh_public_key": "ssh-ed25519 AAAA wiz",
			"do_api_token": "dop_v1_wiz",
			"do_region": "ams3",
			"do_default_size": "s-2vcpu-4gb-intel",
			"do_default_image": "ubuntu-24-04-x64",
			"do_ssh_key_id": "wiz-key",
		}
		args.update(over)
		return args

	def test_stages_provider_only_when_tls_off(self) -> None:
		stages = setup.get_setup_stages(self._do_args())
		self.assertEqual(len(stages), 1)

	def test_stages_include_tls_when_checked(self) -> None:
		# Checkboxes arrive as the string "1" from the wizard.
		args = self._do_args(setup_tls="1")
		stages = setup.get_setup_stages(args)
		self.assertEqual(len(stages), 2)

	def test_provider_stage_applies_setters(self) -> None:
		args = self._do_args()
		for stage in setup.get_setup_stages(args):
			for task in stage["tasks"]:
				task["fn"](task["args"])
		self.assertEqual(frappe.db.get_single_value("Atlas Settings", "region"), "blr1")
		self.assertEqual(frappe.db.get_single_value("DigitalOcean Settings", "region"), "ams3")
		self.assertEqual(frappe.db.get_single_value("DigitalOcean Settings", "ssh_key_id"), "wiz-key")

	def test_truthy_normalizes_wizard_checkbox(self) -> None:
		self.assertTrue(setup._truthy("1"))
		self.assertTrue(setup._truthy("true"))
		self.assertFalse(setup._truthy("0"))
		self.assertFalse(setup._truthy(""))
		self.assertFalse(setup._truthy(None))

	def test_acme_environment_maps_to_directory_url(self) -> None:
		# Staging is the default (None lets setup_tls_layer apply it).
		self.assertIsNone(
			setup._resolve_acme_url({"acme_environment": "Staging (untrusted, no rate limits)"})
		)
		self.assertIsNone(setup._resolve_acme_url({}))
		self.assertEqual(
			setup._resolve_acme_url({"acme_environment": "Production (trusted)"}),
			setup.LETS_ENCRYPT_PRODUCTION,
		)
		self.assertEqual(
			setup._resolve_acme_url(
				{"acme_environment": "Custom URL", "acme_directory_url": "https://acme.test/dir"}
			),
			"https://acme.test/dir",
		)


class TestWizardDiscover(IntegrationTestCase):
	"""`wizard_discover` probes the vendor with just-typed creds and returns a catalog
	for the slide pick-lists. The clients are mocked (no network); we assert the auth
	gate, the slug→option mapping, and that failures come back as a toast, not a raise."""

	def setUp(self) -> None:
		# A successful probe now upserts Provider Size / Provider Image rows; clean them
		# up so the persisted catalog never leaks between tests.
		self.addCleanup(_cleanup)

	def test_digitalocean_returns_constant_catalog_after_auth(self) -> None:
		fake_client = MagicMock()
		fake_client.verify_credentials.return_value = {
			"email": "ops@acme.com",
			"rate_limit": 5000,
			"rate_remaining": 4900,
		}
		with patch("atlas.atlas.digitalocean.DigitalOceanClient", return_value=fake_client):
			result = setup.wizard_discover("DigitalOcean", {"api_token": "dop_v1_x"})
		self.assertTrue(result["ok"])
		self.assertIn("ops@acme.com", result["account_label"])
		# DO's catalog is hand-maintained constants — sizes/images come back regardless.
		self.assertIn("s-2vcpu-4gb-intel", [s["value"] for s in result["sizes"]])
		self.assertIn("ubuntu-24-04-x64", [i["value"] for i in result["images"]])
		# A green probe also persists the catalog (no later Refresh needed).
		self.assertTrue(frappe.db.exists("Provider Size", "DigitalOcean/s-2vcpu-4gb-intel"))
		self.assertTrue(frappe.db.exists("Provider Image", "DigitalOcean/ubuntu-24-04-x64"))

	def test_digitalocean_without_token_is_not_ok(self) -> None:
		result = setup.wizard_discover("DigitalOcean", {})
		self.assertFalse(result["ok"])
		self.assertTrue(result["error"])

	def test_digitalocean_bad_token_returns_error_not_raise(self) -> None:
		from atlas.atlas.digitalocean import DigitalOceanError

		fake_client = MagicMock()
		fake_client.verify_credentials.side_effect = DigitalOceanError("401 Unauthorized")
		with patch("atlas.atlas.digitalocean.DigitalOceanClient", return_value=fake_client):
			result = setup.wizard_discover("DigitalOcean", {"api_token": "bad"})
		self.assertFalse(result["ok"])
		self.assertIn("401", result["error"])

	def test_scaleway_maps_offers_os_projects_and_ssh_keys(self) -> None:
		fake_client = MagicMock()
		fake_client.verify_credentials.return_value = {"account_label": "Acme Project"}
		fake_client.list_projects.return_value = [{"id": "proj-uuid", "name": "Acme Project"}]
		fake_client.list_offers.return_value = [
			{"id": "offer-uuid", "name": SCW_SIZE, "price_per_month": {"units": 40, "nanos": 0}}
		]
		fake_client.list_os.return_value = [{"id": "os-uuid", "name": "Ubuntu", "version": "24.04 LTS"}]
		fake_client.list_ssh_keys.return_value = [{"id": "key-uuid", "name": "laptop"}]
		with patch("atlas.atlas.scaleway.ScalewayClient", return_value=fake_client):
			result = setup.wizard_discover(
				"Scaleway",
				{"secret_key": "scw", "zone": "fr-par-2", "project_id": "proj-uuid", "billing": "monthly"},
			)
		self.assertTrue(result["ok"])
		self.assertEqual([p["value"] for p in result["projects"]], ["proj-uuid"])
		self.assertIn(SCW_SIZE, [s["value"] for s in result["sizes"]])
		self.assertIn(SCW_IMAGE, [i["value"] for i in result["images"]])
		self.assertEqual([k["value"] for k in result["ssh_keys"]], ["key-uuid"])
		# The verified live catalog is persisted to Provider Size / Provider Image.
		self.assertTrue(frappe.db.exists("Provider Size", f"Scaleway/{SCW_SIZE}"))
		self.assertTrue(frappe.db.exists("Provider Image", f"Scaleway/{SCW_IMAGE}"))

	def test_scaleway_without_secret_is_not_ok(self) -> None:
		result = setup.wizard_discover("Scaleway", {"zone": "fr-par-2"})
		self.assertFalse(result["ok"])

	def test_self_managed_has_empty_catalog(self) -> None:
		result = setup.wizard_discover("Self-Managed", {})
		self.assertTrue(result["ok"])
		self.assertEqual(result["sizes"], [])


# --- shared cleanup / single snapshot --------------------------------------

_TOUCHED_SINGLES = (
	("Atlas Settings", "provider_type"),
	("Atlas Settings", "region"),
	("Atlas Settings", "ssh_private_key_path"),
	("Atlas Settings", "ssh_public_key"),
	("Atlas Settings", "default_bench_snapshot"),
	("Atlas Settings", "dns_provider_type"),
	("Atlas Settings", "tls_provider_type"),
	("DigitalOcean Settings", "region"),
	("DigitalOcean Settings", "ssh_key_id"),
	("DigitalOcean Settings", "default_size"),
	("DigitalOcean Settings", "default_image"),
	("Scaleway Settings", "zone"),
	("Scaleway Settings", "project_id"),
	("Scaleway Settings", "billing"),
	("Scaleway Settings", "default_size"),
	("Scaleway Settings", "default_image"),
	("Route53 Settings", "access_key_id"),
	("Route53 Settings", "region"),
	("Lets Encrypt Settings", "account_email"),
	("Lets Encrypt Settings", "acme_directory_url"),
)


def _snapshot_singles() -> dict:
	return {(dt, field): frappe.db.get_single_value(dt, field) for dt, field in _TOUCHED_SINGLES}


def _restore_singles(snapshot: dict) -> None:
	for (dt, field), value in snapshot.items():
		frappe.db.set_single_value(dt, field, value, update_modified=False)
	frappe.db.commit()


def _cleanup() -> None:
	for name in (
		"DigitalOcean/s-2vcpu-4gb-intel",
		f"Scaleway/{SCW_SIZE}",
		"Scaleway/EM-TYPO-NVME",
	):
		if frappe.db.exists("Provider Size", name):
			frappe.delete_doc("Provider Size", name, force=True, ignore_permissions=True)
	for name in ("DigitalOcean/ubuntu-24-04-x64", f"Scaleway/{SCW_IMAGE}"):
		if frappe.db.exists("Provider Image", name):
			frappe.delete_doc("Provider Image", name, force=True, ignore_permissions=True)
	if frappe.db.exists("Root Domain", "blr1.frappe.dev"):
		frappe.delete_doc("Root Domain", "blr1.frappe.dev", force=True, ignore_permissions=True)
	frappe.db.commit()
