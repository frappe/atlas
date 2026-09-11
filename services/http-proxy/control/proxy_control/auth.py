import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from .config import AuthConfig, ConfigError, load

CONTROL_BEARER_SCHEME = "BearerAuth"
KNOWN_SCOPES = frozenset({"*", "site:*", "domain:*"})
CONSTRAINED_RESOURCES = frozenset({"site", "domain"})
CONSTRAINT_KEYS = frozenset({"prefix", "suffix", "names"})
bearer = HTTPBearer(
	scheme_name=CONTROL_BEARER_SCHEME,
	bearerFormat="password or JWT",
	description="Use the regional proxy password or a valid JWT.",
	auto_error=False,
)


@dataclass(frozen=True)
class NameConstraint:
	"""The resource names one authority can reach."""

	prefix: str = ""
	suffix: str = ""
	names: frozenset[str] = frozenset()

	def matches(self, name: str) -> bool:
		"""Report whether one resource name is inside this constraint."""
		if name in self.names:
			return True
		if not self.prefix and not self.suffix:
			return False

		return name.startswith(self.prefix) and name.endswith(self.suffix)


@dataclass(frozen=True)
class Authorization:
	"""The verified Proxy authority for one request."""

	scopes: frozenset[str]
	constraints: dict[str, NameConstraint] = field(default_factory=dict)

	def require(self, resource: str, action: str, name: str | None = None) -> None:
		"""Refuse a request outside this authority."""
		if not self._has_scope(resource, action) or not self._matches_name(resource, name):
			raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

	def require_unconstrained(self, resource: str, action: str) -> None:
		"""Refuse a full-map operation when the authority has a resource constraint."""
		self.require(resource, action)
		if resource in self.constraints:
			raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

	def filter(self, resource: str, values: dict[str, str]) -> dict[str, str]:
		"""Return only the map values that this authority can read."""
		self.require(resource, "read")
		return {name: address for name, address in values.items() if self._matches_name(resource, name)}

	def _has_scope(self, resource: str, action: str) -> bool:
		return bool(self.scopes & {"*", f"{resource}:*", f"{resource}:{action}"})

	def _matches_name(self, resource: str, name: str | None) -> bool:
		constraint = self.constraints.get(resource)
		if constraint is None or name is None:
			return True

		return constraint.matches(name)


class Authentication:
	"""Authenticate bearer passwords and issuer-bound JWTs."""

	def __init__(self, path: Path | None = None) -> None:
		self.path = path
		self._jwks_client: PyJWKClient | None = None
		self._jwks_url = ""

	def require(self, authorization: str | None = None) -> Authorization:
		scheme, _, token = (authorization or "").partition(" ")
		if scheme.lower() != "bearer" or not token:
			raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

		auth = self._auth()
		if self._matches_password(auth, token):
			return Authorization(frozenset({"*"}))

		verified = self._jwt_authorization(auth, token)
		if verified is not None:
			return verified

		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

	def require_request(
		self, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
	) -> Authorization:
		"""Authenticate one API request."""
		authorization = f"{credentials.scheme} {credentials.credentials}" if credentials else None
		return self.require(authorization)

	def _auth(self) -> AuthConfig:
		"""Return the current credentials."""
		try:
			return load(self.path).auth
		except ConfigError:
			return AuthConfig()

	def _matches_password(self, auth: AuthConfig, password: str) -> bool:
		password_hashes = [auth.password_hash]
		if time.time() <= auth.previous_password_valid_until:
			password_hashes.append(auth.previous_password_hash)

		for password_hash in password_hashes:
			if not password_hash:
				continue
			try:
				if bcrypt.checkpw(password.encode(), password_hash.encode()):
					return True
			except ValueError:
				continue

		return False

	def _jwt_authorization(self, auth: AuthConfig, token: str) -> Authorization | None:
		if not auth.jwks_url or not auth.jwks_audience_id or not auth.jwks_issuers:
			return None
		try:
			header = jwt.get_unverified_header(token)
			key_id = header.get("kid")
			if not isinstance(key_id, str) or header.get("alg") != "EdDSA":
				return None

			issuer = _issuer_for_key_id(key_id, auth.jwks_issuers)
			if issuer is None:
				return None

			signing_key = PyJWKClient.match_kid(self._jwks_client_instance(auth).get_signing_keys(), key_id)
			if signing_key is None or signing_key.algorithm_name != "EdDSA":
				return None

			claims = jwt.decode(
				token,
				signing_key.key,
				algorithms=["EdDSA"],
				audience=auth.jwks_audience_id,
				issuer=issuer,
				options={"require": ["iss", "sub", "aud", "scope", "iat", "exp"]},
			)
			if claims.get("aud") != auth.jwks_audience_id:
				return None
			return _authorization_from_claims(claims)
		except jwt.PyJWTError, ValueError, TypeError:
			return None

	def _jwks_client_instance(self, auth: AuthConfig) -> PyJWKClient:
		"""Return a JWKS client for the configured URL."""
		if self._jwks_client is None or self._jwks_url != auth.jwks_url:
			self._jwks_client = PyJWKClient(auth.jwks_url, headers={"User-Agent": "atlas-proxy-control"})
			self._jwks_url = auth.jwks_url

		return self._jwks_client


def _issuer_for_key_id(key_id: str, issuers: tuple[str, ...]) -> str | None:
	for issuer in issuers:
		if key_id.startswith(f"{issuer}:"):
			return issuer
	return None


def _authorization_from_claims(claims: dict[str, object]) -> Authorization | None:
	subject = claims.get("sub")
	scope = claims.get("scope")
	if not isinstance(subject, str) or not subject or not isinstance(scope, str):
		return None
	if "tenant" in claims:
		return None
	if not all(_is_timestamp(claims.get(name)) for name in ("iat", "exp")):
		return None
	if "nbf" in claims and not _is_timestamp(claims["nbf"]):
		return None

	scopes = frozenset(scope.split())
	if not scopes or not scopes <= KNOWN_SCOPES:
		return None

	constraints = _name_constraints(claims.get("constraints", {}))
	if constraints is None:
		return None

	return Authorization(scopes, constraints)


def _name_constraints(value: object) -> dict[str, NameConstraint] | None:
	"""Return one constraint for each constrained resource, or None when a claim is not usable."""
	if not isinstance(value, dict) or set(value) - CONSTRAINED_RESOURCES:
		return None

	constraints = {}
	for resource, claim in value.items():
		constraint = _name_constraint(claim)
		if constraint is None:
			return None
		constraints[resource] = constraint

	return constraints


def _name_constraint(claim: object) -> NameConstraint | None:
	"""Return one validated name constraint, or None when the claim is not usable."""
	if not isinstance(claim, dict) or not claim or set(claim) - CONSTRAINT_KEYS:
		return None

	patterns: dict[str, str] = {}
	for key in ("prefix", "suffix"):
		if key not in claim:
			continue
		pattern = claim[key]
		if not isinstance(pattern, str) or not pattern:
			return None
		patterns[key] = pattern

	names = claim.get("names", [])
	if not isinstance(names, list) or ("names" in claim and not names):
		return None
	if not all(isinstance(name, str) and name for name in names):
		return None

	return NameConstraint(patterns.get("prefix", ""), patterns.get("suffix", ""), frozenset(names))


def _is_timestamp(value: object) -> bool:
	return isinstance(value, int | float) and not isinstance(value, bool)
