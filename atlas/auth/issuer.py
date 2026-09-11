from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

TOKEN_LIFETIME = timedelta(minutes=5)


def initialize_signing_key(settings: AtlasSettings) -> bool:
	"""Create a regional signing key when Atlas has no key for this region."""
	prefix = f"{settings.issuer}:"
	private_key = settings.get_password("jwt_signing_private_key", raise_exception=False)
	if private_key and (settings.jwt_signing_key_id or "").startswith(prefix):
		return False

	key = Ed25519PrivateKey.generate()
	private_key = key.private_bytes(
		Encoding.PEM,
		PrivateFormat.PKCS8,
		NoEncryption(),
	).decode()
	key_id = f"{prefix}{uuid4()}"
	settings.jwt_signing_private_key = private_key
	settings.jwt_signing_key_id = key_id
	return True


def issue_token(
	settings: AtlasSettings,
	*,
	audience: str,
	subject: str,
	scope: str,
	tenant: str | None = None,
	constraints: dict[str, dict[str, str | list[str]]] | None = None,
	lifetime: timedelta = TOKEN_LIFETIME,
) -> str:
	"""Return one token that carries the audience, subject, and authority the caller asks for."""
	now = datetime.now(UTC)
	claims: dict[str, Any] = {
		"iss": settings.issuer,
		"sub": subject,
		"aud": audience,
		"scope": scope,
		"iat": now,
		"nbf": now,
		"exp": now + lifetime,
	}

	if tenant is not None:
		claims["tenant"] = tenant
	if constraints:
		claims["constraints"] = constraints

	private_key = settings.get_password("jwt_signing_private_key", raise_exception=False)
	if not private_key or not settings.jwt_signing_key_id:
		raise RuntimeError("Atlas has no JSON Web Token signing key.")

	return jwt.encode(
		claims,
		private_key,
		algorithm="EdDSA",
		headers={"kid": settings.jwt_signing_key_id},
	)
