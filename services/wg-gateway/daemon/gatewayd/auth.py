"""Authenticate one request with an issuer-bound Ed25519 JWT.

The validation flow mirrors the HTTP proxy control daemon: a bearer JWT must
name an EdDSA key, the key id prefix selects the issuer, the signing key comes
from the Atlas JWKS, and a `tenant` claim is refused. Only the scope grammar
differs: this daemon grants peer and gateway scopes, and carries no name
constraints.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

BEARER_SCHEME = "BearerAuth"
JWKS_USER_AGENT = "atlas-wg-gateway"
KNOWN_SCOPES = frozenset({"*", "peers:*", "peers:read", "peers:update", "gateway:read"})
REQUIRED_CLAIMS = ["iss", "sub", "aud", "scope", "iat", "exp"]

bearer = HTTPBearer(
	scheme_name=BEARER_SCHEME,
	bearerFormat="JWT",
	description="Use a JWT for this gateway audience.",
	auto_error=False,
)


@dataclass(frozen=True)
class AuthConfig:
	"""The Atlas signing authority this daemon trusts."""

	jwks_url: str = ""
	jwks_audience_id: str = ""
	jwks_issuers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Authorization:
	"""The verified authority for one request."""

	scopes: frozenset[str]

	def require(self, resource: str, action: str) -> None:
		"""Refuse a request outside this authority."""
		if not self._has_scope(resource, action):
			raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

	def _has_scope(self, resource: str, action: str) -> bool:
		return bool(self.scopes & {"*", f"{resource}:*", f"{resource}:{action}"})


class Authentication:
	"""Authenticate bearer tokens against the Atlas JWKS."""

	def __init__(self) -> None:
		self._jwks_client: PyJWKClient | None = None
		self._jwks_url = ""

	def require(self, authorization: str | None = None) -> Authorization:
		"""Return the authority of one request, or refuse it."""
		scheme, _, token = (authorization or "").partition(" ")
		if scheme.lower() != "bearer" or not token:
			raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

		auth = self._auth()
		granted = self._jwt_authorization(auth, token)
		if granted is None:
			raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

		return granted

	def require_request(
		self, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
	) -> Authorization:
		"""Authenticate one API request."""
		authorization = f"{credentials.scheme} {credentials.credentials}" if credentials else None
		return self.require(authorization)

	def _auth(self) -> AuthConfig:
		"""Return the trusted authority, refusing an incomplete environment."""
		issuers = tuple(
			issuer for issuer in (os.environ.get("WG_GATEWAY_JWKS_ISSUERS", "").split(",")) if issuer
		)
		return AuthConfig(
			jwks_url=os.environ.get("WG_GATEWAY_JWKS_URL", ""),
			jwks_audience_id=os.environ.get("WG_GATEWAY_AUDIENCE_ID", ""),
			jwks_issuers=issuers,
		)

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
				options={"require": REQUIRED_CLAIMS},
			)
			if claims.get("aud") != auth.jwks_audience_id:
				return None
			return _authorization_from_claims(claims)
		except (jwt.PyJWTError, ValueError, TypeError):
			return None

	def _jwks_client_instance(self, auth: AuthConfig) -> PyJWKClient:
		"""Return a JWKS client for the configured URL."""
		if self._jwks_client is None or self._jwks_url != auth.jwks_url:
			self._jwks_client = PyJWKClient(auth.jwks_url, headers={"User-Agent": JWKS_USER_AGENT})
			self._jwks_url = auth.jwks_url

		return self._jwks_client


authentication = Authentication()
GatewayAuthorization = Annotated[Authorization, Depends(authentication.require_request)]


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

	return Authorization(scopes)


def _is_timestamp(value: object) -> bool:
	return isinstance(value, (int, float)) and not isinstance(value, bool)
