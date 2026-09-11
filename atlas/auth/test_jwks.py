import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from frappe.tests import UnitTestCase
from jwt.algorithms import OKPAlgorithm

from atlas.auth.jwks import (
	JWKSError,
	_build_trusted_keys,
	sync_central_jwks,
	trusted_keys,
	validate_central_jwks,
)


def private_key_pem() -> str:
	return (
		Ed25519PrivateKey.generate().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
	)


def public_jwk(key_id: str) -> dict:
	private_key = Ed25519PrivateKey.generate()
	jwk = json.loads(OKPAlgorithm.to_jwk(private_key.public_key()))
	jwk.update({"kid": key_id, "alg": "EdDSA", "use": "sig"})
	return jwk


class TestCentralJWKS(UnitTestCase):
	def test_a_valid_key_set_is_accepted(self) -> None:
		key = public_jwk("central:key-1")

		self.assertEqual(validate_central_jwks({"keys": [key]}), [key])

	def test_private_duplicate_and_unscoped_keys_are_refused(self) -> None:
		private = public_jwk("central:key-1") | {"d": "private"}
		duplicate = public_jwk("central:key-1")
		for document in (
			{"keys": [private]},
			{"keys": [duplicate, duplicate]},
			{"keys": [public_jwk("key-1")]},
			{"keys": []},
		):
			with self.assertRaises(JWKSError):
				validate_central_jwks(document)

	def test_a_successful_sync_replaces_the_stored_key_set(self) -> None:
		settings = Mock(central_jwks_url="https://central.example.com/jwks")
		response = Mock()
		response.json.return_value = {"keys": [public_jwk("central:key-1")]}
		with (
			patch("atlas.auth.jwks.frappe.get_single", return_value=settings),
			patch("atlas.auth.jwks.requests.get", return_value=response),
		):
			self.assertTrue(sync_central_jwks())

		stored = json.loads(settings.db_set.call_args.args[1])
		self.assertEqual(stored["keys"][0]["kid"], "central:key-1")

	def test_a_failed_sync_keeps_the_stored_key_set(self) -> None:
		settings = Mock(central_jwks_url="https://central.example.com/jwks")
		response = Mock()
		response.json.return_value = {"keys": [public_jwk("wrong:key-1")]}
		with (
			patch("atlas.auth.jwks.frappe.get_single", return_value=settings),
			patch("atlas.auth.jwks.requests.get", return_value=response),
			patch("atlas.auth.jwks.frappe.log_error"),
		):
			self.assertFalse(sync_central_jwks())

		settings.db_set.assert_not_called()


class TestTrustedKeys(UnitTestCase):
	def setUp(self) -> None:
		_build_trusted_keys.clear_cache()

	def tearDown(self) -> None:
		_build_trusted_keys.clear_cache()

	def build_settings(self, key_id: str = "atlas:42:key-1", central_key_id: str | None = None):
		central_jwks = json.dumps({"keys": [public_jwk(central_key_id)]}) if central_key_id else ""
		return SimpleNamespace(
			central_jwks=central_jwks,
			issuer="atlas:42",
			jwt_signing_key_id=key_id,
			modified="2026-09-11 00:00:00",
			get_password=lambda *args, **kwargs: private_key_pem(),
		)

	def test_the_regional_key_set_contains_central_and_atlas_keys(self) -> None:
		settings = self.build_settings(central_key_id="central:key-1")
		with patch("atlas.auth.jwks.frappe.get_cached_doc", return_value=settings):
			keys = trusted_keys()

		self.assertEqual([key["kid"] for key in keys.document["keys"]], ["central:key-1", "atlas:42:key-1"])
		self.assertIsNotNone(keys.key("atlas:42:key-1"))
		self.assertIsNone(keys.key("central:unknown"))

	def test_one_revision_is_built_once(self) -> None:
		settings = self.build_settings()
		with patch("atlas.auth.jwks.frappe.get_cached_doc", return_value=settings):
			self.assertIs(trusted_keys(), trusted_keys())

	def test_a_changed_revision_rebuilds_the_key_set(self) -> None:
		settings = self.build_settings()
		with patch("atlas.auth.jwks.frappe.get_cached_doc", return_value=settings):
			first = trusted_keys()
			settings.modified = "2026-09-11 01:00:00"
			settings.jwt_signing_key_id = "atlas:42:key-2"
			second = trusted_keys()

		self.assertIsNot(first, second)
		self.assertIsNotNone(second.key("atlas:42:key-2"))

	def test_the_atlas_key_must_use_the_current_region_namespace(self) -> None:
		settings = self.build_settings(key_id="central:key-1")
		with (
			patch("atlas.auth.jwks.frappe.get_cached_doc", return_value=settings),
			self.assertRaises(JWKSError),
		):
			trusted_keys()

	def test_a_failure_is_not_cached(self) -> None:
		settings = self.build_settings(key_id="central:key-1")
		with patch("atlas.auth.jwks.frappe.get_cached_doc", return_value=settings):
			for _ in range(2):
				with self.assertRaises(JWKSError):
					trusted_keys()
