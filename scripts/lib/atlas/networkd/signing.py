"""ed25519 signatures for ANCP — envelope (§19.1), per-record (§19.3), and
operator-signed newcomer introduction (§19.5).

ANCP rides plain UDP on the public IPv6 endpoint (spec §5, §19 intro) — there
is no WireGuard transport binding. Authentication happens in three layered
checks, all in this module's primitives:

1. **Envelope signature** (§19.1) — `sign_envelope` / `verify_envelope` —
   every `Message` is whole-envelope-signed by its `sender`'s ed25519 private
   key. The receiver verifies the signature against its cached pubkey for that
   `sender` (`daemon.signing_pubkey_cache`), dropping the datagram before any
   payload work on any failure.
2. **Per-record signature** (§19.3) — `sign` / `verify` (with `kind=
   "membership"|"ownership"`) — every piggybacked `MembershipRecord` and
   `OwnershipAdvertisement` is signed by its *origin*'s key, independent of
   the relay that forwarded it. Stops a relay fabricating or mutating records
   "from" another origin.
3. **Introduction certificate** (§19.5) — `sign_introduction` /
   `verify_introduction` — the newcomer's first `MembershipAdvertisement`
   carries an operator-signed binding `({host_id, signing_public_key,
   generation=1})`; existing hosts verify against the operator's provision
   pubkey (seeded at §19.4) and only then absorb the newcomer's self-asserted
   signing pubkey.

The signing keypair is generated on first boot alongside the wg keypair
(`keys.ensure_signing_keypair`); its public half rides the Membership Record,
so peers can verify every subsequent record against the origin's published
signing pubkey at apply time.

This module is pure above the keypair files (signing keys themselves are
materialized in `keys.ensure_signing_keypair`; the operator key is materialized
controller-side and written to every host's
`/etc/atlas-networkd/operator-public-key` at provision time).
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


# --- envelope + introduction signatures (spec §19.1, §19.5) -----------------
#
# The envelope signature (§19.1) authenticates the SENDER of an ANCP datagram —
# `Message.signature` over the canonical body of {type, sender,
# signing_public_key, payload}. The introduction signature (§19.5) authenticates
# a newcomer's identity binding (`host_id ↔ signing_public_key` at generation 1)
# against the operator's provision pubkey, not the sender's own key — a
# separate kind so a signature over one domain can't be replayed across the
# other. Both reuse `_canonical_body`; the kind tag is the domain separator.


def sign_envelope(body: dict, signing_priv_b64: str) -> str:
	"""Sign the canonical envelope body ({type, sender, signing_public_key,
	payload}) with the sender's ed25519 signing key. Returns the base64
	signature the wire serializer embeds as `Message.signature`."""
	import base64

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

	canonical = _canonical_body(body, kind="envelope")
	priv = _load_private(signing_priv_b64)
	return base64.b64encode(priv.sign(canonical)).decode("utf-8")


def verify_envelope(body: dict, sig_b64: str, signing_pub_b64: str) -> None:
	"""Verify an envelope signature against the sender's signing pubkey. Raises
	`SignatureError` on any failure. The `body` is the same dict the signer
	passed to `sign_envelope` (no `signature` field needed); `sig_b64` is the
	wire signature extracted by `wire.from_bytes`."""
	import base64

	from cryptography.exceptions import InvalidSignature
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

	if not isinstance(sig_b64, str) or not sig_b64:
		raise SignatureError("envelope carries no signature")
	try:
		raw = base64.b64decode(sig_b64)
	except Exception as exc:
		raise SignatureError(f"envelope signature is not base64: {exc}") from exc
	canonical = _canonical_body(body, kind="envelope")
	try:
		Ed25519PublicKey.from_public_bytes(_b64decode_pub(signing_pub_b64)).verify(raw, canonical)
	except InvalidSignature as exc:
		raise SignatureError("invalid envelope signature") from exc
	except Exception as exc:
		raise SignatureError(f"envelope verify failed: {exc}") from exc


def sign_introduction(introduction_body: dict, operator_priv_b64: str) -> str:
	"""Sign the canonical introduction body `{host_id, signing_public_key,
	generation}` with the operator's provision private key (spec §19.5). The
	result rides the newcomer's first MembershipAdvertisement as
	`Message.introduction_signature`; existing hosts verify it against the
	operator's provision pubkey (seeded to every host, §19.4)."""
	import base64

	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

	canonical = _canonical_body(introduction_body, kind="introduction")
	priv = _load_private(operator_priv_b64)
	return base64.b64encode(priv.sign(canonical)).decode("utf-8")


def verify_introduction(introduction_body: dict, sig_b64: str, operator_pub_b64: str) -> None:
	"""Verify an introduction certificate against the operator's provision
	pubkey (spec §19.5). Raises `SignatureError` on any failure."""
	import base64

	from cryptography.exceptions import InvalidSignature
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

	if not isinstance(sig_b64, str) or not sig_b64:
		raise SignatureError("introduction carries no signature")
	try:
		raw = base64.b64decode(sig_b64)
	except Exception as exc:
		raise SignatureError(f"introduction signature is not base64: {exc}") from exc
	canonical = _canonical_body(introduction_body, kind="introduction")
	try:
		Ed25519PublicKey.from_public_bytes(_b64decode_pub(operator_pub_b64)).verify(raw, canonical)
	except InvalidSignature as exc:
		raise SignatureError("invalid introduction signature") from exc
	except Exception as exc:
		raise SignatureError(f"introduction verify failed: {exc}") from exc


def sign_detached(body: bytes, signing_priv_b64: str) -> str:
	"""Sign arbitrary bytes with an ed25519 private key, returning the base64
	detached signature. Unlike `sign`/`sign_envelope` there is NO canonical-dict
	shaping or `_kind` domain tag — the signer commits to the exact `body` bytes
	the verifier will re-read. Used for the operator-signed seed file (spec §19.4
	/ §9.2 — the seed is the sole trust root and is signed as its literal on-disk
	bytes so controller and host agree byte-for-byte)."""
	import base64

	priv = _load_private(signing_priv_b64)
	return base64.b64encode(priv.sign(body)).decode("utf-8")


def verify_detached(body: bytes, sig_b64: str, signing_pub_b64: str) -> None:
	"""Verify a detached signature (from `sign_detached`) over the exact `body`
	bytes against a base64 ed25519 pubkey. Raises `SignatureError` on any failure
	(empty/missing signature, bad base64, wrong key, tampered body, malformed
	pubkey). The verifier commits to the literal bytes — no canonicalization."""
	import base64

	from cryptography.exceptions import InvalidSignature
	from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

	if not isinstance(sig_b64, str) or not sig_b64:
		raise SignatureError("detached signature is missing")
	try:
		raw = base64.b64decode(sig_b64)
	except Exception as exc:
		raise SignatureError(f"detached signature is not base64: {exc}") from exc
	try:
		Ed25519PublicKey.from_public_bytes(_b64decode_pub(signing_pub_b64)).verify(raw, body)
	except InvalidSignature as exc:
		raise SignatureError("invalid detached signature") from exc
	except Exception as exc:
		raise SignatureError(f"detached verify failed: {exc}") from exc


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
	"sign_detached",
	"sign_envelope",
	"sign_introduction",
	"verify",
	"verify_detached",
	"verify_envelope",
	"verify_introduction",
]
