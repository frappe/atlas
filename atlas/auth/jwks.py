from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import frappe
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from frappe.utils.caching import site_cache
from jwt import PyJWK
from jwt.algorithms import OKPAlgorithm

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

CENTRAL_ISSUER = "central"
KEY_CACHE_SECONDS = 600
JWKS_PATH = "/api/atlas/jwks.json"
JWKS_TIMEOUT_SECONDS = 10
MAXIMUM_CENTRAL_KEYS = 100
JWK_FIELDS = frozenset({"alg", "crv", "d", "key_ops", "kid", "kty", "use", "x"})


class JWKSError(ValueError):
	"""The JSON Web Key Set is not safe to publish or use."""


@dataclass(frozen=True, slots=True)
class TrustedKeys:
	"""The verification keys of one Atlas Settings revision."""

	document: dict[str, list[dict[str, Any]]]
	keys: dict[str, PyJWK]

	def key(self, key_id: str) -> PyJWK | None:
		"""Return the verification key of one key ID."""
		return self.keys.get(key_id)


def trusted_keys() -> TrustedKeys:
	"""Return the keys that this region trusts, rebuilt when Atlas Settings changes."""
	settings = frappe.get_cached_doc("Atlas Settings")

	return _build_trusted_keys(str(settings.modified), settings.central_jwks or "")


@site_cache(ttl=KEY_CACHE_SECONDS, maxsize=4)
def _build_trusted_keys(modified: str, central_jwks: str) -> TrustedKeys:
	settings = frappe.get_cached_doc("Atlas Settings")
	keys = [*_stored_central_keys(central_jwks), atlas_public_jwk(settings)]

	return TrustedKeys(
		document={"keys": keys},
		keys={key["kid"]: PyJWK.from_dict(key) for key in keys},
	)


def atlas_public_jwk(settings: AtlasSettings) -> dict[str, Any]:
	"""Return the public JSON Web Key for the current Atlas issuer."""
	private_key = settings.get_password("jwt_signing_private_key", raise_exception=False)
	key_id = settings.jwt_signing_key_id
	if not private_key or not key_id:
		raise JWKSError("Atlas has no JSON Web Token signing key.")
	prefix = f"{settings.issuer}:"
	if not isinstance(key_id, str) or not key_id.startswith(prefix) or not key_id.removeprefix(prefix):
		raise JWKSError("The Atlas signing key ID must use the regional issuer namespace.")

	from cryptography.hazmat.primitives.serialization import load_pem_private_key

	key = load_pem_private_key(private_key.encode(), password=None)
	if not isinstance(key, Ed25519PrivateKey):
		raise JWKSError("The Atlas JSON Web Token signing key must use Ed25519.")
	jwk = OKPAlgorithm.to_jwk(key.public_key(), as_dict=True)
	jwk.update({"alg": "EdDSA", "kid": key_id, "use": "sig"})
	return jwk


def sync_central_jwks() -> bool:
	"""Store a valid Central key set and keep the stored set after a failure."""
	settings = frappe.get_single("Atlas Settings")
	if not settings.central_jwks_url:
		if settings.central_jwks:
			settings.db_set("central_jwks", "", update_modified=False)
			frappe.clear_document_cache("Atlas Settings")
		return True

	try:
		response = requests.get(
			settings.central_jwks_url,
			headers={"Accept": "application/json", "User-Agent": "atlas"},
			timeout=JWKS_TIMEOUT_SECONDS,
		)
		response.raise_for_status()
		keys = validate_central_jwks(response.json())
	except (requests.RequestException, ValueError, TypeError) as error:
		frappe.log_error(str(error), "Central JWKS synchronization failed")
		return False

	settings.db_set("central_jwks", json.dumps({"keys": keys}, separators=(",", ":")), update_modified=False)
	frappe.clear_document_cache("Atlas Settings")
	return True


def validate_central_jwks(document: Any) -> list[dict[str, Any]]:
	"""Return the usable Ed25519 signature keys from one Central key set."""
	if not isinstance(document, dict) or set(document) != {"keys"}:
		raise JWKSError("The Central JWKS response must contain only a keys array.")

	keys = document["keys"]
	if not isinstance(keys, list) or not 1 <= len(keys) <= MAXIMUM_CENTRAL_KEYS:
		raise JWKSError(f"The Central JWKS response needs 1 through {MAXIMUM_CENTRAL_KEYS} keys.")

	validated = []
	key_ids: set[str] = set()
	for key in keys:
		validated_key = _validate_central_jwk(key)
		key_id = validated_key["kid"]
		if key_id in key_ids:
			raise JWKSError(f"The Central JWKS response contains duplicate key ID {key_id}.")
		key_ids.add(key_id)
		validated.append(validated_key)

	return validated


def issuer_for_key_id(key_id: str, region_id: int) -> str | None:
	"""Return the issuer that owns a namespaced key ID."""
	if key_id.startswith(f"{CENTRAL_ISSUER}:"):
		return CENTRAL_ISSUER

	atlas_issuer = f"atlas:{region_id}"
	if key_id.startswith(f"{atlas_issuer}:"):
		return atlas_issuer

	return None


def _validate_central_jwk(key: Any) -> dict[str, Any]:
	if not isinstance(key, dict):
		raise JWKSError("Each Central JSON Web Key must be an object.")
	if set(key) - JWK_FIELDS:
		raise JWKSError("A Central JSON Web Key contains an unsupported field.")
	if "d" in key:
		raise JWKSError("A Central JSON Web Key must not contain private key data.")
	if key.get("kty") != "OKP" or key.get("crv") != "Ed25519" or key.get("alg") != "EdDSA":
		raise JWKSError("Each Central JSON Web Key must be an Ed25519 EdDSA key.")
	if key.get("use", "sig") != "sig":
		raise JWKSError("Each Central JSON Web Key must be a signature key.")
	if "key_ops" in key and key["key_ops"] != ["verify"]:
		raise JWKSError("Each Central JSON Web Key must permit signature verification.")

	key_id = key.get("kid")
	if (
		not isinstance(key_id, str)
		or not key_id.startswith(f"{CENTRAL_ISSUER}:")
		or not key_id.removeprefix(f"{CENTRAL_ISSUER}:")
		or len(key_id) > 256
	):
		raise JWKSError("Each Central key ID must start with central:.")

	try:
		PyJWK.from_dict(key)
	except (ValueError, TypeError) as error:
		raise JWKSError(f"Central key {key_id} is not valid: {error}") from error

	return dict(key)


def _stored_central_keys(value: str) -> list[dict[str, Any]]:
	if not value:
		return []

	try:
		return validate_central_jwks(json.loads(value))
	except (json.JSONDecodeError, JWKSError) as error:
		raise JWKSError("The stored Central JWKS is not valid.") from error
