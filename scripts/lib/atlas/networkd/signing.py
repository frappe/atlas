"""ed25519 record signatures (spec §19.3 — defense in depth).

The §19.1 transport binding (a record's sender == wg-authenticated MeshID) is
enough under "every host runs the same ANCP code"; §19.3 adds end-to-end
ed25519 record signatures so that a *relay* forwarding another host's record
can't tamper with it. The signing key is generated on first boot alongside the
wg keypair (Issue A); its public half rides the Membership Record (one more
field), so peers can verify every record's signature against the origin's
published signing pubkey at apply time.

Verification is per-apply (`gossip._apply_record` calls `verify_record`); a
forged origin (a relay that rewrote a record's `host_id` to its own) fails the
signature check and is dropped + logged. The cost is ~64 B per record + a few
µs per verify.

This module is pure above the keypair file (signing keys themselves are
materialized in `keys.ensure_signing_keypair`, alongside the wg keypair). The
two halves are split so the apply-path verifier never touches the key file
directly — `verify_record(record, signing_pubkey_b64)` is a pure function.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# The signature payload is the same dict the wire `(de)serializers` produce
# (`wire.membership_to_dict` / `wire.ownership_to_dict`), WITHOUT the
# signature field — so the bytes the signer signs are byte-identical to the
# bytes the verifier reconstructs from the wire record. We strip the signature
# field before signing and before verifying, the standard detached-signature
# pattern. The dict is sorted-keys + compact separators so two peers encode
# the same record to the same bytes.


# --- exceptions ------------------------------------------------------------


class SignatureError(RuntimeError):
	"""A record's signature failed verification. Raised by `verify_record`;
	caught by the gossip apply path which drops the record (§19.3) + emits a
	counter event (§18.2 / §20 observability)."""


# --- record signing payloads (the canonical-bytes shape) --------------------


def _membership_signing_payload(record_dict: dict) -> bytes:
	"""The bytes the origin signs for a Membership Record. The dict carries the
	standard fields (host_id, kind, state, endpoint, wg_public_key,
	mesh_address, generation) + a signing_public_key field (the origin's
	ed25519 pubkey, base64 — rides the Membership Record so a verifier can look
	up the right pubkey). The signature is over the dict WITHOUT the signature
	field itself."""
	import json

	body = {k: v for k, v in record_dict.items() if k != "signature"}
	return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _ownership_signing_payload(record_dict: dict) -> bytes:
	"""Same shape for an Ownership Advertisement."""
	import json

	body = {k: v for k, v in record_dict.items() if k != "signature"}
	return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


# --- sign / verify ----------------------------------------------------------


def sign(record_dict: dict, signing_priv_b64: str, kind: str) -> str:
	"""Sign a record dict with the origin's ed25519 signing key. Returns the
	base64 signature that the wire serializer attaches as `record["signature"]`.

	`kind` is `"membership"` or `"ownership"` — a domain-separation tag inside
	the signed payload so a signature over a Membership Record can't be reused
	on an Ownership Advertisement (defense against a future relay bug). The wire
	serializer appends `kind` to the signed body."""
	import base64
	import json

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

	body = _canonical_body(record_dict, kind)
	priv = _load_private(signing_priv_b64)
	raw = priv.sign(body)
	return base64.b64encode(raw).decode("utf-8")


def verify(record_dict: dict, signing_pub_b64: str, kind: str) -> None:
	"""Verify a record's signature against the origin's published signing
	pubkey. Raises `SignatureError` on any failure (no signature field, bad
	base64, wrong key, tampered body). The caller applies the record only if
	this returns normally."""
	import base64

	from cryptography.exceptions import InvalidSignature
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

	sig_str = record_dict.get("signature")
	if not isinstance(sig_str, str):
		raise SignatureError("record carries no signature")
	try:
		raw = base64.b64decode(sig_str)
	except Exception as exc:
		raise SignatureError(f"signature is not base64: {exc}") from exc
	body = _canonical_body(record_dict, kind)
	try:
		Ed25519PublicKey.from_public_bytes(_b64decode_pub(signing_pub_b64)).verify(raw, body)
	except InvalidSignature as exc:
		raise SignatureError("invalid ed25519 signature") from exc
	except Exception as exc:
		# A malformed pubkey (wrong length, not base64) — surface loud, don't
		# silently accept.
		raise SignatureError(f"verify failed: {exc}") from exc


def _canonical_body(record_dict: dict, kind: str) -> bytes:
	"""The bytes the signer signs / the verifier reconstructs. Includes `kind`
	as a domain separator; strips the `signature` field; canonical (sorted keys
	+ compact separators) so two peers produce identical bytes for the same
	record."""
	import json

	body = {k: v for k, v in record_dict.items() if k != "signature"}
	body["_kind"] = kind
	return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_private(priv_b64: str):
	"""Decode a base64 ed25519 private seed into an `Ed25519PrivateKey`."""
	import base64

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

	raw = base64.b64decode(priv_b64)
	return Ed25519PrivateKey.from_private_bytes(raw)


def _b64decode_pub(pub_b64: str) -> bytes:
	"""Decode a base64 ed25519 public key (32 raw bytes)."""
	import base64

	return base64.b64decode(pub_b64)


# --- key generation (used by keys.ensure_signing_keypair) -------------------


def generate_keypair_raw() -> tuple[bytes, bytes]:
	"""Generate a fresh ed25519 keypair. Returns `(priv_raw_32B, pub_raw_32B)`.
	The caller base64s both halves before writing to disk / advertising."""
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

	priv = Ed25519PrivateKey.generate()
	pub = priv.public_key()
	from cryptography.hazmat.primitives import serialization

	priv_raw = priv.private_bytes(
		encoding=serialization.Encoding.Raw,
		format=serialization.PrivateFormat.Raw,
		encryption_algorithm=serialization.NoEncryption(),
	)
	pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
	return priv_raw, pub_raw


__all__ = [
	"SignatureError",
	"generate_keypair_raw",
	"sign",
	"verify",
]
