from types import SimpleNamespace

import jwt
from frappe.tests import UnitTestCase

from atlas.auth.issuer import initialize_signing_key, issue_token


class TestIssuer(UnitTestCase):
	def test_a_signing_key_uses_the_region_namespace(self) -> None:
		settings = SimpleNamespace(
			issuer="atlas:42",
			jwt_signing_key_id=None,
			jwt_signing_private_key=None,
			get_password=lambda *args, **kwargs: None,
		)

		self.assertTrue(initialize_signing_key(settings))
		self.assertTrue(settings.jwt_signing_key_id.startswith("atlas:42:"))
		self.assertIn("BEGIN PRIVATE KEY", settings.jwt_signing_private_key)

	def test_a_token_carries_the_requested_authority(self) -> None:
		settings = _signed_settings()

		token = issue_token(
			settings,
			audience="atlas-proxy:42",
			subject="pdf-renderer",
			scope="site:*",
			constraints={"site": {"suffix": "-svc"}},
		)
		claims = jwt.decode(token, options={"verify_signature": False})

		self.assertEqual(claims["iss"], "atlas:42")
		self.assertEqual(claims["aud"], "atlas-proxy:42")
		self.assertEqual(claims["sub"], "pdf-renderer")
		self.assertEqual(claims["scope"], "site:*")
		self.assertEqual(claims["constraints"], {"site": {"suffix": "-svc"}})
		self.assertNotIn("tenant", claims)

	def test_a_token_carries_a_tenant_only_when_asked(self) -> None:
		settings = _signed_settings()

		token = issue_token(
			settings,
			audience="atlas-admin:42",
			subject="operator",
			scope="*",
			tenant="7",
		)
		claims = jwt.decode(token, options={"verify_signature": False})

		self.assertEqual(claims["tenant"], "7")
		self.assertNotIn("constraints", claims)


def _signed_settings() -> SimpleNamespace:
	"""Return settings that hold one usable regional signing key."""
	settings = SimpleNamespace(
		issuer="atlas:42",
		jwt_signing_key_id=None,
		jwt_signing_private_key=None,
	)
	settings.get_password = lambda *args, **kwargs: settings.jwt_signing_private_key
	initialize_signing_key(settings)
	return settings
