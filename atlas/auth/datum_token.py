from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from atlas.auth import issuer

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

TOKEN_LIFETIME = timedelta(hours=1)


def issue_datum_token(
	settings: AtlasSettings,
	resource_id: str,
	*,
	signing_key: tuple[str, str] | None = None,
	lifetime: timedelta = TOKEN_LIFETIME,
) -> str:
	"""Return one write token for resource_id, signed with the regional key.

	Datum verifies the same key set Atlas already publishes at its JWKS route,
	so no separate signing key exists for this. Pass signing_key when issuing
	several tokens in a row, so the key is only fetched and decrypted once.
	"""
	now = datetime.now(UTC)
	claims = {
		"iss": settings.issuer,
		"aud": f"atlas-datum:{settings.region_id}",
		"resource_id": resource_id,
		"access": ["write"],
		"iat": now,
		"nbf": now,
		"exp": now + lifetime,
	}

	private_key, key_id = signing_key or issuer.signing_key(settings)
	return issuer.sign(claims, private_key, key_id)
