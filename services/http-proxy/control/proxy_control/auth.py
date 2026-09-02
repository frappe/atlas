from pathlib import Path
from typing import Annotated, ClassVar

import bcrypt
import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient


class Authentication:
	"""Check a bearer token: a password against a bcrypt hash, or a JWT against JWKS.

	The password hash comes from the htpasswd file only. JWKS is asymmetric only,
	so a published public key can never double as an HMAC secret. It applies only
	when both a JWKS URL and an audience are configured.
	"""

	_JWKS_ALGORITHMS: ClassVar[tuple[str, ...]] = (
		"RS256",
		"RS384",
		"RS512",
		"ES256",
		"ES384",
		"ES512",
		"PS256",
		"PS384",
		"PS512",
		"EdDSA",
	)

	def __init__(
		self,
		path: Path,
		jwks_url: str | None = None,
		jwks_audience: str | None = None,
	) -> None:
		self.path = path
		self.jwks_url = jwks_url
		self.jwks_audience = jwks_audience
		self._jwks_client: PyJWKClient | None = None

	def require(self, authorization: Annotated[str | None, Header()] = None) -> None:
		scheme, _, token = (authorization or "").partition(" ")
		if scheme.lower() != "bearer" or not token:
			raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
		if self._matches_password(token) or self._matches_jwks(token):
			return
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

	def _matches_password(self, password: str) -> bool:
		password_hash = self._file_password_hash()
		if not password_hash:
			return False
		try:
			return bcrypt.checkpw(password.encode(), password_hash.encode())
		except ValueError:
			return False

	def _file_password_hash(self) -> str | None:
		try:
			line = next(
				line
				for line in self.path.read_text().splitlines()
				if line.strip() and not line.lstrip().startswith("#")
			)
		except OSError, StopIteration:
			return None
		_, separator, password_hash = line.partition(":")
		return password_hash if separator and password_hash else None

	def _matches_jwks(self, token: str) -> bool:
		if not self.jwks_url or not self.jwks_audience:
			return False
		try:
			kid = jwt.get_unverified_header(token).get("kid")
			if not isinstance(kid, str):
				return False
			signing_key = PyJWKClient.match_kid(self._jwks_client_instance().get_signing_keys(), kid)
			if signing_key is None:
				return False
			jwt.decode(
				token,
				signing_key.key,
				algorithms=self._JWKS_ALGORITHMS,
				audience=self.jwks_audience,
				options={"require": ["exp", "aud"], "verify_aud": True},
			)
			return True
		except jwt.PyJWTError:
			return False

	def _jwks_client_instance(self) -> PyJWKClient:
		if self._jwks_client is None:
			if self.jwks_url is None:
				raise RuntimeError("JWKS URL is not configured")
			self._jwks_client = PyJWKClient(
				self.jwks_url,
				headers={"User-Agent": "atlas-proxy-control"},
			)
		return self._jwks_client
