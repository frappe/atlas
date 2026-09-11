import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import frappe
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from frappe.tests import UnitTestCase
from jwt import PyJWK
from jwt.algorithms import OKPAlgorithm

from atlas.auth.identity import current_identity
from atlas.auth.jwks import TrustedKeys
from atlas.auth.request import authenticate_token
from atlas.auth.token import TokenValidator
from atlas.auth.user import CENTRAL_TENANT_USER

AUDIENCE = "atlas-admin:42"
ATLAS_ISSUER = "atlas:42"


def build_token(
	private_key,
	*,
	key_id: str,
	issuer: str,
	subject: str,
	tenant: str,
	audience: str = AUDIENCE,
	scope: str = "*",
	expires_in: int = 300,
	constraints: dict | None = None,
) -> str:
	"""Return one signed Atlas API token."""
	now = datetime.now(UTC)
	claims = {
		"iss": issuer,
		"sub": subject,
		"aud": audience,
		"scope": scope,
		"tenant": tenant,
		"iat": now,
		"exp": now + timedelta(seconds=expires_in),
	}
	if constraints is not None:
		claims["constraints"] = constraints
	return jwt.encode(claims, private_key, algorithm="EdDSA", headers={"kid": key_id})


def public_jwk(private_key, key_id: str) -> PyJWK:
	jwk = json.loads(OKPAlgorithm.to_jwk(private_key.public_key()))
	jwk.update({"kid": key_id, "alg": "EdDSA", "use": "sig"})
	return PyJWK.from_dict(jwk)


class TestToken(UnitTestCase):
	def setUp(self) -> None:
		self.private_key = Ed25519PrivateKey.generate()

	def validate(self, token: str, key_id: str):
		settings = SimpleNamespace(region_id=42, admin_audience_id=AUDIENCE)
		keys = TrustedKeys(document={"keys": []}, keys={key_id: public_jwk(self.private_key, key_id)})
		with (
			patch("frappe.get_cached_doc", return_value=settings),
			patch("atlas.auth.token.trusted_keys", return_value=keys),
		):
			return TokenValidator().claims(token)

	def test_central_has_unrestricted_tenant_authority(self) -> None:
		key_id = "central:key-1"
		token = build_token(
			self.private_key,
			key_id=key_id,
			issuer="central",
			subject="central",
			tenant="*",
		)

		self.assertEqual(self.validate(token, key_id)["tenant"], "*")

	def test_a_regional_token_keeps_its_signed_subject_and_tenant(self) -> None:
		key_id = "atlas:42:key-1"
		token = build_token(
			self.private_key,
			key_id=key_id,
			issuer=ATLAS_ISSUER,
			subject="operator",
			tenant="7",
		)

		self.assertEqual(self.validate(token, key_id)["tenant"], "7")

	def test_an_atlas_key_cannot_claim_to_be_central(self) -> None:
		key_id = "atlas:42:key-1"
		token = build_token(
			self.private_key,
			key_id=key_id,
			issuer="central",
			subject="central",
			tenant="*",
		)

		self.assertIsNone(self.validate(token, key_id))

	def test_another_region_is_not_trusted(self) -> None:
		key_id = "atlas:43:key-1"
		token = build_token(
			self.private_key,
			key_id=key_id,
			issuer="atlas:43",
			subject="operator",
			tenant="7",
		)

		self.assertIsNone(self.validate(token, key_id))

	def test_invalid_authority_is_refused(self) -> None:
		"""The tenant grammar belongs to AtlasIdentity, so this covers scope and constraints."""
		key_id = "atlas:42:key-1"
		for changes in ({"scope": "image:*"}, {"constraints": {"site": {"suffix": "-svc"}}}):
			token = build_token(
				self.private_key,
				key_id=key_id,
				issuer=ATLAS_ISSUER,
				subject="operator",
				tenant="7",
				scope=changes.get("scope", "*"),
				constraints=changes.get("constraints"),
			)
			self.assertIsNone(self.validate(token, key_id))

	def test_wrong_multiple_audience_and_expired_tokens_are_refused(self) -> None:
		key_id = "central:key-1"
		for audience, expires_in in (
			("atlas-proxy:42", 300),
			([AUDIENCE, "atlas-proxy:42"], 300),
			(AUDIENCE, -1),
		):
			token = build_token(
				self.private_key,
				key_id=key_id,
				issuer="central",
				subject="central",
				tenant="*",
				audience=audience,
				expires_in=expires_in,
			)
			self.assertIsNone(self.validate(token, key_id))


class TestTokenSession(UnitTestCase):
	def setUp(self) -> None:
		self.previous_user = frappe.session.user
		self.previous_identity = getattr(frappe.local, "atlas_identity", None)
		frappe.set_user("Guest")
		frappe.local.atlas_identity = None

	def tearDown(self) -> None:
		frappe.local.atlas_identity = self.previous_identity
		frappe.set_user(self.previous_user)

	def test_a_central_token_becomes_the_every_tenant_user(self) -> None:
		claims = {"iss": "central", "sub": "central", "tenant": "*", "scope": "*"}
		with (
			patch("frappe.get_request_header", return_value="Bearer a-token"),
			patch.object(TokenValidator, "claims", return_value=claims),
			patch("atlas.auth.request.frappe.set_user") as set_user,
		):
			authenticate_token()

		set_user.assert_called_once_with(CENTRAL_TENANT_USER)
		self.assertTrue(current_identity().is_central)

	def test_a_regional_token_becomes_its_tenant_user(self) -> None:
		claims = {"iss": "atlas:42", "sub": "cargo", "tenant": "7", "scope": "*"}
		with (
			patch("frappe.get_request_header", return_value="Bearer a-token"),
			patch.object(TokenValidator, "claims", return_value=claims),
			patch("atlas.auth.request.ensure_tenant_user", return_value="tenant-7@atlas.local") as ensure,
			patch("atlas.auth.request.frappe.set_user") as set_user,
		):
			authenticate_token()

		ensure.assert_called_once_with("7")
		set_user.assert_called_once_with("tenant-7@atlas.local")
		self.assertEqual(current_identity().tenant, "7")

	def test_an_unusable_tenant_claim_keeps_the_guest_session(self) -> None:
		claims = {"iss": "atlas:42", "sub": "cargo", "tenant": "not-a-tenant", "scope": "*"}
		with (
			patch("frappe.get_request_header", return_value="Bearer a-token"),
			patch.object(TokenValidator, "claims", return_value=claims),
			patch("atlas.auth.request.frappe.set_user") as set_user,
		):
			authenticate_token()

		set_user.assert_not_called()
		self.assertIsNone(current_identity())

	def test_an_invalid_token_keeps_the_guest_session(self) -> None:
		with (
			patch("frappe.get_request_header", return_value="Bearer a-token"),
			patch.object(TokenValidator, "claims", return_value=None),
			patch("atlas.auth.request.frappe.set_user") as set_user,
		):
			authenticate_token()

		set_user.assert_not_called()

	def test_a_signed_in_session_is_never_replaced(self) -> None:
		frappe.set_user("Administrator")
		with patch.object(TokenValidator, "claims") as claims:
			authenticate_token()

		claims.assert_not_called()
