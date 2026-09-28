from datetime import UTC, datetime
from types import SimpleNamespace

import jwt
from frappe.tests import UnitTestCase

from atlas.auth import issuer
from atlas.auth.datum_token import issue_datum_token
from atlas.auth.issuer import initialize_signing_key


class TestDatumToken(UnitTestCase):
	def test_a_token_carries_the_resource_and_write_access(self) -> None:
		settings = _signed_settings()

		token = issue_datum_token(settings, "vm-00001")
		claims = jwt.decode(token, options={"verify_signature": False})

		self.assertEqual(claims["iss"], "atlas:42")
		self.assertEqual(claims["aud"], "atlas-datum:42")
		self.assertEqual(claims["resource_id"], "vm-00001")
		self.assertEqual(claims["access"], ["write"])

	def test_bundle_tokens_share_the_exact_issue_time_and_expiry(self) -> None:
		settings = _signed_settings()
		issued_at = datetime(2026, 1, 1, tzinfo=UTC)
		for resource in ("host-1", "vm-1"):
			token = issue_datum_token(settings, resource, issued_at=issued_at)
			claims = jwt.decode(token, options={"verify_signature": False})
			self.assertEqual(claims["iat"], int(issued_at.timestamp()))
			self.assertEqual(claims["exp"], int(issued_at.timestamp()) + 3600)

	def test_a_pre_fetched_signing_key_skips_the_settings_lookup(self) -> None:
		settings = _signed_settings()
		key = issuer.signing_key(settings)
		settings.get_password = lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("should not re-fetch the signing key")
		)

		token = issue_datum_token(settings, "vm-00001", signing_key=key)
		claims = jwt.decode(token, options={"verify_signature": False})

		self.assertEqual(claims["resource_id"], "vm-00001")

	def test_a_token_needs_a_signing_key(self) -> None:
		settings = SimpleNamespace(
			issuer="atlas:42",
			region_id=42,
			jwt_signing_key_id=None,
			jwt_signing_private_key=None,
			get_password=lambda *args, **kwargs: None,
		)

		self.assertRaises(RuntimeError, issue_datum_token, settings, "vm-00001")


def _signed_settings() -> SimpleNamespace:
	"""Return settings that hold one usable regional signing key."""
	settings = SimpleNamespace(
		issuer="atlas:42",
		region_id=42,
		jwt_signing_key_id=None,
		jwt_signing_private_key=None,
	)
	settings.get_password = lambda *args, **kwargs: settings.jwt_signing_private_key
	initialize_signing_key(settings)
	return settings
