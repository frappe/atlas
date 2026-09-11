import json
import time
from pathlib import Path

import bcrypt
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from jwt import PyJWK
from jwt.algorithms import OKPAlgorithm

from proxy_control.auth import Authentication, Authorization, NameConstraint

AUDIENCE = "atlas-proxy:42"
ATLAS_ISSUER = "atlas:42"
PASSWORD = "correct-horse"
PREVIOUS_PASSWORD = "previous-horse"


def write_config(
	path: Path,
	has_jwks: bool = True,
	password: str = "",
	previous_password: str = "",
	previous_password_valid_until: int = 0,
) -> Path:
	"""Write a configuration with the credentials that a test needs."""
	lines = [
		"[tls]",
		'wildcard_domain = "*.par-1.example.com"',
		'fullchain_pem = "leaf"',
		'private_key_pem = "key"',
	]
	if has_jwks or password or previous_password:
		lines.extend(["", "[auth]"])
	if password:
		lines.append(f'password_hash = "{bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()}"')
	if previous_password:
		lines.extend(
			[
				f'previous_password_hash = "{bcrypt.hashpw(previous_password.encode(), bcrypt.gensalt()).decode()}"',
				f"previous_password_valid_until = {previous_password_valid_until}",
			]
		)
	if has_jwks:
		lines.extend(
			[
				'jwks_url = "https://issuer.example.com/jwks.json"',
				f'jwks_audience_id = "{AUDIENCE}"',
				f'jwks_issuers = ["central", "{ATLAS_ISSUER}"]',
			]
		)
	path.write_text("\n".join(lines) + "\n")
	return path


def test_require_accepts_the_current_password(tmp_path):
	path = write_config(tmp_path / "proxy-control.toml", has_jwks=False, password=PASSWORD)

	Authentication(path).require(f"Bearer {PASSWORD}")


def test_require_accepts_the_previous_password_before_expiry(tmp_path):
	path = write_config(
		tmp_path / "proxy-control.toml",
		has_jwks=False,
		password=PASSWORD,
		previous_password=PREVIOUS_PASSWORD,
		previous_password_valid_until=int(time.time()) + 600,
	)

	Authentication(path).require(f"Bearer {PREVIOUS_PASSWORD}")


def test_require_rejects_the_previous_password_after_expiry(tmp_path):
	path = write_config(
		tmp_path / "proxy-control.toml",
		has_jwks=False,
		password=PASSWORD,
		previous_password=PREVIOUS_PASSWORD,
		previous_password_valid_until=int(time.time()) - 1,
	)

	with pytest.raises(HTTPException):
		Authentication(path).require(f"Bearer {PREVIOUS_PASSWORD}")


def test_require_rejects_an_incorrect_password(tmp_path):
	path = write_config(tmp_path / "proxy-control.toml", has_jwks=False, password=PASSWORD)

	with pytest.raises(HTTPException):
		Authentication(path).require("Bearer incorrect-password")


class _FakeJWKClient:
	def __init__(self, keys: list[PyJWK]):
		self.keys = keys

	def get_signing_keys(self) -> list[PyJWK]:
		return self.keys


def _key_pair() -> Ed25519PrivateKey:
	return Ed25519PrivateKey.generate()


def _jwk(public_key, key_id: str) -> PyJWK:
	jwk_data = json.loads(OKPAlgorithm.to_jwk(public_key))
	jwk_data.update({"kid": key_id, "alg": "EdDSA", "use": "sig"})
	return PyJWK.from_json(json.dumps(jwk_data))


def _token(
	private_key,
	key_id: str,
	audience: str = AUDIENCE,
	ttl_seconds: int = 3600,
	issuer: str = ATLAS_ISSUER,
	subject: str = "atlas",
	scope: str = "site:*",
	constraints: dict | None = None,
	tenant: str | None = None,
) -> str:
	now = int(time.time())
	payload = {
		"iss": issuer,
		"sub": subject,
		"aud": audience,
		"scope": scope,
		"iat": now,
		"exp": now + ttl_seconds,
	}
	payload["constraints"] = constraints if constraints is not None else {"site": {"suffix": "-svc"}}
	if tenant is not None:
		payload["tenant"] = tenant
	return jwt.encode(payload, private_key, algorithm="EdDSA", headers={"kid": key_id})


def _authentication(path: Path, jwk: PyJWK) -> Authentication:
	write_config(path)
	authentication = Authentication(path)
	authentication._jwks_client = _FakeJWKClient([jwk])
	authentication._jwks_url = "https://issuer.example.com/jwks.json"
	return authentication


def test_require_accepts_a_valid_jwks_token(tmp_path):
	private_key = _key_pair()
	key_id = f"{ATLAS_ISSUER}:key-1"
	authentication = _authentication(tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), key_id))

	authorization = authentication.require(authorization=f"Bearer {_token(private_key, key_id)}")

	authorization.require("site", "update", "pdf-svc")


def test_the_signed_claim_selects_the_site_suffix(tmp_path):
	private_key = _key_pair()
	key_id = f"{ATLAS_ISSUER}:key-1"
	authentication = _authentication(tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), key_id))
	token = _token(private_key, key_id, constraints={"site": {"suffix": "-worker"}})

	authorization = authentication.require(authorization=f"Bearer {token}")

	authorization.require("site", "update", "render-worker")
	with pytest.raises(HTTPException):
		authorization.require("site", "update", "render-svc")


def test_an_empty_constraint_allows_every_site_name(tmp_path):
	private_key = _key_pair()
	key_id = f"{ATLAS_ISSUER}:key-1"
	authentication = _authentication(tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), key_id))
	token = _token(private_key, key_id, constraints={})

	authorization = authentication.require(authorization=f"Bearer {token}")

	authorization.require_unconstrained("site", "update")
	authorization.require("site", "update", "any-site-name")


def test_an_unrestricted_scope_grants_every_resource(tmp_path):
	private_key = _key_pair()
	key_id = "central:key-1"
	authentication = _authentication(tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), key_id))
	token = _token(private_key, key_id, issuer="central", subject="central", scope="*", constraints={})

	authorization = authentication.require(authorization=f"Bearer {token}")

	authorization.require("domain", "update", "customer.example.com")
	authorization.require_unconstrained("site", "update")


@pytest.mark.parametrize("authorization", [None, "token", "Basic token"])
def test_require_rejects_a_missing_bearer_token(tmp_path, authorization):
	with pytest.raises(HTTPException):
		Authentication(write_config(tmp_path / "proxy-control.toml")).require(authorization)


def test_require_rejects_the_cluster_password(tmp_path):
	path = write_config(tmp_path / "proxy-control.toml", has_jwks=False)
	with path.open("a") as target:
		target.write(
			'\n[cluster]\nnode_id = "proxy-001"\npassword = "cluster-secret"\n'
			'peers = [{ node_id = "proxy-001", address = "https://proxy-001.example.com" }]\n'
		)

	with pytest.raises(HTTPException):
		Authentication(path).require("Bearer cluster-secret")


def test_require_reads_a_changed_password_without_a_restart(tmp_path):
	path = write_config(tmp_path / "proxy-control.toml", has_jwks=False, password=PASSWORD)
	authentication = Authentication(path)
	authentication.require(f"Bearer {PASSWORD}")

	write_config(path, has_jwks=False, password="rotated-password")

	authentication.require("Bearer rotated-password")
	with pytest.raises(HTTPException):
		authentication.require(f"Bearer {PASSWORD}")


def test_require_rejects_a_missing_or_malformed_config(tmp_path):
	for path in (tmp_path / "missing.toml", tmp_path / "malformed.toml"):
		if path.name == "malformed.toml":
			path.write_text("[auth\n")
		with pytest.raises(HTTPException):
			Authentication(path).require("Bearer token")


@pytest.mark.parametrize(
	("key_id", "audience", "ttl_seconds"),
	[
		("atlas:42:unknown-key", AUDIENCE, 3600),
		("atlas:42:key-1", "another-audience", 3600),
		("atlas:42:key-1", AUDIENCE, -60),
	],
)
def test_require_rejects_an_invalid_jwks_token(tmp_path, key_id, audience, ttl_seconds):
	private_key = _key_pair()
	authentication = _authentication(
		tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), "atlas:42:key-1")
	)
	token = _token(private_key, key_id, audience, ttl_seconds)

	with pytest.raises(HTTPException):
		authentication.require(f"Bearer {token}")


def test_a_key_cannot_claim_another_issuer(tmp_path):
	private_key = _key_pair()
	key_id = "atlas:42:key-1"
	authentication = _authentication(tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), key_id))
	token = _token(private_key, key_id, issuer="central", scope="*")

	with pytest.raises(HTTPException):
		authentication.require(f"Bearer {token}")


def test_a_prefix_and_a_suffix_narrow_one_resource(tmp_path):
	private_key = _key_pair()
	key_id = f"{ATLAS_ISSUER}:key-1"
	authentication = _authentication(tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), key_id))
	token = _token(private_key, key_id, constraints={"site": {"prefix": "erp-", "suffix": "-svc"}})

	authorization = authentication.require(authorization=f"Bearer {token}")

	authorization.require("site", "update", "erp-pdf-svc")
	for name in ("erp-pdf", "pdf-svc"):
		with pytest.raises(HTTPException):
			authorization.require("site", "update", name)


def test_a_name_list_grants_the_listed_names_beside_a_pattern(tmp_path):
	private_key = _key_pair()
	key_id = f"{ATLAS_ISSUER}:key-1"
	authentication = _authentication(tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), key_id))
	token = _token(private_key, key_id, constraints={"site": {"prefix": "erp-", "names": ["legacy-pdf"]}})

	authorization = authentication.require(authorization=f"Bearer {token}")

	authorization.require("site", "update", "erp-pdf-svc")
	authorization.require("site", "update", "legacy-pdf")
	with pytest.raises(HTTPException):
		authorization.require("site", "update", "pdf-svc")


def test_a_domain_scope_carries_its_own_constraint(tmp_path):
	private_key = _key_pair()
	key_id = f"{ATLAS_ISSUER}:key-1"
	authentication = _authentication(tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), key_id))
	token = _token(
		private_key,
		key_id,
		scope="domain:*",
		constraints={"domain": {"names": ["www.customer.com"]}},
	)

	authorization = authentication.require(authorization=f"Bearer {token}")

	authorization.require("domain", "update", "www.customer.com")
	assert authorization.filter("domain", {"www.customer.com": "::1", "api.customer.com": "::2"}) == {
		"www.customer.com": "::1"
	}
	with pytest.raises(HTTPException):
		authorization.require("domain", "update", "api.customer.com")
	with pytest.raises(HTTPException):
		authorization.require_unconstrained("domain", "update")
	with pytest.raises(HTTPException):
		authorization.require("site", "update", "pdf-svc")


def test_a_site_constraint_leaves_other_resources_open(tmp_path):
	private_key = _key_pair()
	key_id = f"{ATLAS_ISSUER}:key-1"
	authentication = _authentication(tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), key_id))
	token = _token(private_key, key_id, scope="site:* domain:*", constraints={"site": {"suffix": "-svc"}})

	authorization = authentication.require(authorization=f"Bearer {token}")

	authorization.require("domain", "update", "www.customer.com")
	authorization.require_unconstrained("domain", "update")
	with pytest.raises(HTTPException):
		authorization.require("site", "update", "customer")


def test_a_constraint_applies_to_an_unrestricted_scope(tmp_path):
	private_key = _key_pair()
	key_id = f"{ATLAS_ISSUER}:key-1"
	authentication = _authentication(tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), key_id))
	token = _token(private_key, key_id, scope="*", constraints={"site": {"suffix": "-svc"}})

	authorization = authentication.require(authorization=f"Bearer {token}")

	authorization.require("site", "update", "pdf-svc")
	authorization.require("domain", "update", "customer.example.com")
	with pytest.raises(HTTPException):
		authorization.require("site", "update", "customer")


def test_an_administrative_token_is_refused_by_the_proxy(tmp_path):
	private_key = _key_pair()
	key_id = f"{ATLAS_ISSUER}:key-1"
	authentication = _authentication(tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), key_id))
	token = _token(private_key, key_id, tenant="1")

	with pytest.raises(HTTPException):
		authentication.require(f"Bearer {token}")


@pytest.mark.parametrize(
	("scope", "constraints"),
	[
		("unknown:*", None),
		("site:*", {"unknown": {"suffix": "-svc"}}),
		("site:*", {"site": {"unknown": "-svc"}}),
		("site:*", {"site": {"suffix": ""}}),
		("site:*", {"site": {"prefix": ""}}),
		("site:*", {"site": {}}),
		("site:*", {"site": {"names": []}}),
		("site:*", {"site": {"names": ["pdf-svc", ""]}}),
		("site:*", {"site": {"names": "pdf-svc"}}),
	],
)
def test_unknown_or_malformed_authority_is_rejected(tmp_path, scope, constraints):
	private_key = _key_pair()
	key_id = "atlas:42:key-1"
	authentication = _authentication(tmp_path / "proxy-control.toml", _jwk(private_key.public_key(), key_id))
	token = _token(private_key, key_id, scope=scope, constraints=constraints)

	with pytest.raises(HTTPException):
		authentication.require(f"Bearer {token}")


def test_a_site_constraint_filters_and_restricts_names():
	authorization = Authorization(frozenset({"site:*"}), {"site": NameConstraint(suffix="-svc")})

	assert authorization.filter("site", {"pdf-svc": "::1", "customer": "::2"}) == {"pdf-svc": "::1"}
	authorization.require("site", "update", "pdf-svc")
	with pytest.raises(HTTPException) as failure:
		authorization.require("site", "update", "customer")
	assert failure.value.status_code == 403


def test_a_constrained_token_cannot_replace_a_complete_map():
	authorization = Authorization(frozenset({"site:*"}), {"site": NameConstraint(suffix="-svc")})

	with pytest.raises(HTTPException) as failure:
		authorization.require_unconstrained("site", "update")
	assert failure.value.status_code == 403


def test_site_authority_does_not_grant_domain_authority():
	authorization = Authorization(frozenset({"site:*"}))

	with pytest.raises(HTTPException) as failure:
		authorization.require("domain", "update", "customer.example.com")
	assert failure.value.status_code == 403
