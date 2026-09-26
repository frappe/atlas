from __future__ import annotations

import base64
import time
import uuid
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from gatewayd.auth import Authentication
from jwt import PyJWK

AUDIENCE = "atlas-wg-gateway:42"
ISSUER = "atlas:42"
CENTRAL = "central"
JWKS_URL = "https://atlas.example.com/api/atlas/jwks.json"


def b64(raw: bytes) -> str:
	return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class Signer:
	"""One Ed25519 key with its published JWKS entry."""

	def __init__(self, issuer: str) -> None:
		self.issuer = issuer
		self.key = Ed25519PrivateKey.generate()
		self.key_id = f"{issuer}:{uuid.uuid4()}"

	def jwk(self, key_id: str) -> dict:
		return {
			"kty": "OKP",
			"crv": "Ed25519",
			"alg": "EdDSA",
			"kid": key_id,
			"use": "sig",
			"x": b64(self.key.public_key().public_bytes_raw()),
		}

	def mint(self, key_id: str | None = None, **overrides) -> str:
		now = int(time.time())
		claims = {
			"iss": self.issuer,
			"sub": "central",
			"aud": AUDIENCE,
			"scope": "peers:*",
			"iat": now,
			"exp": now + 300,
		}
		claims.update(overrides)
		return jwt.encode(claims, self.key, algorithm="EdDSA", headers={"kid": key_id or self.key_id})


class StubKeys:
	"""Serve the published keys the way PyJWKClient would."""

	def __init__(self, published: list[dict]) -> None:
		self._published = published

	def get_signing_keys(self) -> list[PyJWK]:
		return [PyJWK(entry) for entry in self._published]


class StubJWKClient:
	"""Match a published key by its id, like PyJWKClient.match_kid."""

	def __init__(self, published: list[dict]) -> None:
		self._published = published

	def get_signing_keys(self) -> list[PyJWK]:
		return [PyJWK(entry) for entry in self._published]

	@staticmethod
	def match_kid(keys, kid):
		for key in keys:
			if key.key_id == kid:
				return key
		return None


@pytest.fixture
def atlas() -> Signer:
	return Signer(ISSUER)


@pytest.fixture
def central() -> Signer:
	return Signer(CENTRAL)


@pytest.fixture
def authentication(atlas: Signer, central: Signer) -> Authentication:
	published = [atlas.jwk(atlas.key_id), central.jwk(central.key_id)]
	instance = Authentication()
	instance._jwks_client = StubJWKClient(published)
	instance._jwks_url = JWKS_URL
	instance._published = published
	return instance


@pytest.fixture(autouse=True)
def environment() -> None:
	with patch.dict(
		"os.environ",
		{
			"WG_GATEWAY_JWKS_URL": JWKS_URL,
			"WG_GATEWAY_AUDIENCE_ID": AUDIENCE,
			"WG_GATEWAY_JWKS_ISSUERS": f"{CENTRAL},{ISSUER}",
		},
	):
		yield


def bearer(token: str) -> str:
	return f"Bearer {token}"


class TestTokenValidation:
	def test_a_scoped_token_is_accepted(self, authentication, atlas: Signer) -> None:
		granted = authentication.require(bearer(atlas.mint()))
		assert granted.scopes == frozenset({"peers:*"})

	def test_a_central_token_is_accepted(self, authentication, central: Signer) -> None:
		token = central.mint(iss=CENTRAL, sub="central-service")
		assert authentication.require(bearer(token)).scopes == frozenset({"peers:*"})

	def test_a_wrong_audience_is_refused(self, authentication, atlas: Signer) -> None:
		with pytest.raises(HTTPException) as error:
			authentication.require(bearer(atlas.mint(aud="atlas-proxy:42")))
		assert error.value.status_code == 401

	def test_an_unknown_audience_is_refused(self, authentication, atlas: Signer) -> None:
		with pytest.raises(HTTPException) as error:
			authentication.require(bearer(atlas.mint(aud="atlas-admin:42")))
		assert error.value.status_code == 401

	def test_a_wrong_issuer_is_refused(self, authentication, atlas: Signer) -> None:
		with pytest.raises(HTTPException) as error:
			authentication.require(bearer(atlas.mint(iss="atlas:99")))
		assert error.value.status_code == 401

	def test_an_expired_token_is_refused(self, authentication, atlas: Signer) -> None:
		now = int(time.time())
		with pytest.raises(HTTPException) as error:
			authentication.require(bearer(atlas.mint(iat=now - 600, exp=now - 300)))
		assert error.value.status_code == 401

	def test_a_tenant_claim_is_refused(self, authentication, atlas: Signer) -> None:
		with pytest.raises(HTTPException) as error:
			authentication.require(bearer(atlas.mint(tenant="1")))
		assert error.value.status_code == 401

	def test_an_unknown_scope_is_refused(self, authentication, atlas: Signer) -> None:
		with pytest.raises(HTTPException) as error:
			authentication.require(bearer(atlas.mint(scope="site:*")))
		assert error.value.status_code == 401

	def test_an_empty_scope_is_refused(self, authentication, atlas: Signer) -> None:
		with pytest.raises(HTTPException) as error:
			authentication.require(bearer(atlas.mint(scope="")))
		assert error.value.status_code == 401

	def test_a_missing_bearer_is_refused(self, authentication) -> None:
		with pytest.raises(HTTPException) as error:
			authentication.require(None)
		assert error.value.status_code == 401

	def test_a_wrong_scheme_is_refused(self, authentication, atlas: Signer) -> None:
		with pytest.raises(HTTPException) as error:
			authentication.require(f"Basic {atlas.mint()}")
		assert error.value.status_code == 401

	def test_a_foreign_signature_is_refused(self, authentication, atlas: Signer, central: Signer) -> None:
		forged = central.mint(iss=ISSUER, sub="central")
		with pytest.raises(HTTPException) as error:
			authentication.require(bearer(forged))
		assert error.value.status_code == 401

	def test_an_unsigned_token_is_refused(self, authentication, atlas: Signer) -> None:
		now = int(time.time())
		token = jwt.encode(
			{
				"iss": ISSUER,
				"sub": "central",
				"aud": AUDIENCE,
				"scope": "peers:*",
				"iat": now,
				"exp": now + 300,
			},
			"",
			algorithm="none",
			headers={"kid": atlas.key_id},
		)
		with pytest.raises(HTTPException) as error:
			authentication.require(bearer(token))
		assert error.value.status_code == 401

	def test_a_missing_authority_env_refuses_everything(self, authentication, atlas: Signer) -> None:
		with patch.dict("os.environ", {"WG_GATEWAY_AUDIENCE_ID": ""}):
			with pytest.raises(HTTPException) as error:
				authentication.require(bearer(atlas.mint()))
		assert error.value.status_code == 401


class TestScopes:
	def test_peers_read_does_not_update(self, authentication, atlas: Signer) -> None:
		granted = authentication.require(bearer(atlas.mint(scope="peers:read")))
		granted.require("peers", "read")
		with pytest.raises(HTTPException) as error:
			granted.require("peers", "update")
		assert error.value.status_code == 403

	def test_gateway_read_does_not_touch_peers(self, authentication, atlas: Signer) -> None:
		granted = authentication.require(bearer(atlas.mint(scope="gateway:read")))
		granted.require("gateway", "read")
		with pytest.raises(HTTPException) as error:
			granted.require("peers", "read")
		assert error.value.status_code == 403

	def test_star_reaches_every_resource(self, authentication, atlas: Signer) -> None:
		granted = authentication.require(bearer(atlas.mint(scope="*")))
		granted.require("peers", "update")
		granted.require("gateway", "read")
