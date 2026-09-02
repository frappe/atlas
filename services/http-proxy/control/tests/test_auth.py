import json
import time
from pathlib import Path

import bcrypt
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt import PyJWK
from jwt.algorithms import RSAAlgorithm

from proxy_control.auth import Authentication

PASSWORD = "correct-horse"
AUDIENCE = "atlas-proxy-control"


@pytest.fixture
def htpasswd_file(tmp_path: Path) -> Path:
	path = tmp_path / "proxy-control.htpasswd"
	password_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt())
	path.write_text(f"admin:{password_hash.decode()}\n")
	return path


def test_require_accepts_correct_password(htpasswd_file):
	Authentication(htpasswd_file).require(authorization=f"Bearer {PASSWORD}")


def test_require_rejects_wrong_password(htpasswd_file):
	with pytest.raises(HTTPException):
		Authentication(htpasswd_file).require(authorization="Bearer wrong-password")


def test_require_rejects_missing_bearer_scheme(htpasswd_file):
	with pytest.raises(HTTPException):
		Authentication(htpasswd_file).require(authorization=PASSWORD)


def test_require_rejects_missing_header(htpasswd_file):
	with pytest.raises(HTTPException):
		Authentication(htpasswd_file).require(authorization=None)


def test_require_rejects_missing_auth_file(tmp_path):
	with pytest.raises(HTTPException):
		Authentication(tmp_path / "missing.htpasswd").require(authorization=f"Bearer {PASSWORD}")


def test_require_rejects_line_without_a_hash(tmp_path):
	path = tmp_path / "proxy-control.htpasswd"
	path.write_text("invalid")

	with pytest.raises(HTTPException):
		Authentication(path).require(authorization=f"Bearer {PASSWORD}")


# nginx/setup.sh writes an empty placeholder file, so the daemon can start before
# the controller sends a credential.
def test_require_rejects_file_with_no_credentials(tmp_path):
	path = tmp_path / "proxy-control.htpasswd"
	path.write_text("# no credentials yet\n\n")

	with pytest.raises(HTTPException):
		Authentication(path).require(authorization=f"Bearer {PASSWORD}")


class _FakeJWKClient:
	def __init__(self, keys: list[PyJWK]):
		self._keys = keys

	def get_signing_keys(self) -> list[PyJWK]:
		return self._keys


def _key_pair() -> rsa.RSAPrivateKey:
	return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(public_key, kid: str) -> PyJWK:
	jwk_data = json.loads(RSAAlgorithm.to_jwk(public_key))
	jwk_data["kid"] = kid
	return PyJWK.from_json(json.dumps(jwk_data))


def _token(private_key, kid: str, audience: str = AUDIENCE, ttl: int = 3600) -> str:
	payload = {"sub": "controller", "aud": audience, "exp": int(time.time()) + ttl}
	return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


def _jwks_auth(htpasswd_file, jwk: PyJWK) -> Authentication:
	auth = Authentication(
		htpasswd_file, jwks_url="https://issuer.example.com/jwks.json", jwks_audience=AUDIENCE
	)
	auth._jwks_client = _FakeJWKClient([jwk])
	return auth


def test_require_accepts_valid_jwks_token(htpasswd_file):
	private_key = _key_pair()
	jwk = _jwk(private_key.public_key(), kid="key-1")
	auth = _jwks_auth(htpasswd_file, jwk)
	token = _token(private_key, kid="key-1")
	auth.require(authorization=f"Bearer {token}")


# A fresh image has the empty placeholder file and no password.
# JWKS must still give access.
def test_require_accepts_valid_jwks_token_when_file_has_no_credentials(tmp_path):
	path = tmp_path / "proxy-control.htpasswd"
	path.write_text("")
	private_key = _key_pair()
	jwk = _jwk(private_key.public_key(), kid="key-1")
	auth = _jwks_auth(path, jwk)
	token = _token(private_key, kid="key-1")
	auth.require(authorization=f"Bearer {token}")


def test_require_rejects_jwks_token_with_wrong_audience(htpasswd_file):
	private_key = _key_pair()
	jwk = _jwk(private_key.public_key(), kid="key-1")
	auth = _jwks_auth(htpasswd_file, jwk)
	token = _token(private_key, kid="key-1", audience="someone-else")
	with pytest.raises(HTTPException):
		auth.require(authorization=f"Bearer {token}")


def test_require_rejects_jwks_token_with_unknown_kid(htpasswd_file):
	private_key = _key_pair()
	jwk = _jwk(private_key.public_key(), kid="key-1")
	auth = _jwks_auth(htpasswd_file, jwk)
	token = _token(private_key, kid="key-does-not-exist")
	with pytest.raises(HTTPException):
		auth.require(authorization=f"Bearer {token}")


def test_require_rejects_expired_jwks_token(htpasswd_file):
	private_key = _key_pair()
	jwk = _jwk(private_key.public_key(), kid="key-1")
	auth = _jwks_auth(htpasswd_file, jwk)
	token = _token(private_key, kid="key-1", ttl=-60)
	with pytest.raises(HTTPException):
		auth.require(authorization=f"Bearer {token}")


def test_jwks_is_ignored_when_not_configured(htpasswd_file):
	private_key = _key_pair()
	token = _token(private_key, kid="key-1")
	with pytest.raises(HTTPException):
		Authentication(htpasswd_file).require(authorization=f"Bearer {token}")
