"""Self-generated WireGuard keypair (spec §7.1 / §8 — **Issue A closeout**).

Storage: 32-byte Curve25519 private scalar at
`/etc/atlas-networkd/wg-private-key` (0600), base64-standard because that's
what `wg set private-key` reads. The public key (also base64) is stored
alongside at `wg-public-key` so the daemon can advertise it on its Membership
Record without re-running `wg pubkey` on every boot.

We deliberately use `wg genkey` + `wg pubkey` (guaranteed present on every
Atlas host — `host-mesh.service` already requires wg) instead of pulling in
`cryptography` (a Frappe dep, not a host dep). Matches the spec's "few deps"
operating principle (#5) and Taste.md's "don't import — copy" rule: `os.urandom`
seed could be clamped by hand, but the public-key base-point multiply is most
honestly done by the WireGuard tools themselves — they already do, on every
host. No new dependency.

`ensure_keypair(private_key_path, public_key_path)` is **idempotent**: if both
files exist with valid keys, it does nothing; otherwise it generates and
writes. The systemd unit (Stage 1b) calls this once before bring-up so the
device-bring-up step can `wg set private-key` against a real key.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_DEFAULT_PRIVATE_KEY_PATH = "/etc/atlas-networkd/wg-private-key"
_DEFAULT_PUBLIC_KEY_PATH = "/etc/atlas-networkd/wg-public-key"
# The ed25519 signing keypair (spec §19.3). Lives alongside the wg keypair at
# first boot; the public half rides the Membership Record (see records.py).
_DEFAULT_SIGNING_PRIV_PATH = "/etc/atlas-networkd/signing-private-key"
_DEFAULT_SIGNING_PUB_PATH = "/etc/atlas-networkd/signing-public-key"


def ensure_keypair(
	private_key_path: str = _DEFAULT_PRIVATE_KEY_PATH,
	public_key_path: str = _DEFAULT_PUBLIC_KEY_PATH,
	*,
	_log_warning=print,
) -> tuple[str, str]:
	"""Idempotently ensure a WireGuard keypair exists on disk.

	Returns `(private_key_b64, public_key_b64)`. The private key file is 0600; the
	public key file is 0644 (it's not secret — it rides Membership Records). Returns
	the cached values if a valid pair already exists; otherwise generates a
	fresh keypair, writes both atomically.

	Raises on `wg genkey`/`wg pubkey` failure (host missing `wireguard-tools`, a
	hardware entropy issue) — fail loud at the boundary (Taste.md), do not fall
	back to a derived / weak key.
	"""
	if _existing_pair_valid(private_key_path, public_key_path):
		return _read_key(private_key_path), _read_key(public_key_path)
	private_b64, public_b64 = _generate_keypair()
	_write_key(private_key_path, private_b64, mode=0o600)
	_write_key(public_key_path, public_b64, mode=0o644)
	return private_b64, public_b64


def ensure_signing_keypair(
	signing_priv_path: str = _DEFAULT_SIGNING_PRIV_PATH,
	signing_pub_path: str = _DEFAULT_SIGNING_PUB_PATH,
) -> tuple[str, str]:
	"""Idempotently ensure an ed25519 signing keypair exists on disk (spec
	§19.3 — defense in depth on top of §19.1's wg-transport binding). Same
	0600/0644 mode posture as the wg keypair. Returns `(priv_b64, pub_b64)`.

	The signing key is NOT derived (Issue A applies equally — a derived signing
	key's seed would be public, defeating the purpose). Generated on first
	boot; regenerated with a fresh key only if either file is absent or the
	public doesn't verify against the private."""
	from . import signing

	if _existing_signing_pair_valid(signing_priv_path, signing_pub_path):
		return _read_key(signing_priv_path), _read_key(signing_pub_path)
	priv_raw, pub_raw = signing.generate_keypair_raw()
	import base64

	priv_b64 = base64.b64encode(priv_raw).decode()
	pub_b64 = base64.b64encode(pub_raw).decode()
	_write_key(signing_priv_path, priv_b64, mode=0o600)
	_write_key(signing_pub_path, pub_b64, mode=0o644)
	return priv_b64, pub_b64


def _existing_pair_valid(private_path: str, public_path: str) -> bool:
	"""True iff both files exist and the public is the legit mate of the private.
	We re-check `_derive_pubkey(private) == public` so a tampered / half-written
	pair (e.g. an interrupted first-boot) is regenerated rather than trusted.
	"""
	if not Path(private_path).exists() or not Path(public_path).exists():
		return False
	private = _read_key(private_path)
	if not _is_valid_private_b64(private):
		return False
	try:
		derived = _derive_pubkey_b64(private)
	except Exception:
		return False
	return derived == _read_key(public_path)


def _generate_keypair() -> tuple[str, str]:
	"""Run `wg genkey` then pipe through `wg pubkey`. The pair is generated
	entirely in `wg`'s own memory; no entropy from us. `capture_output=True`
	holds the bytes inside Python, not in an argv."""
	private = subprocess.run(["wg", "genkey"], capture_output=True, check=True, text=True)
	private_b64 = private.stdout.strip()
	public_b64 = _derive_pubkey_b64(private_b64)
	return private_b64, public_b64


def _derive_pubkey_b64(private_b64: str) -> str:
	"""`echo <priv> | wg pubkey`. Used both to validate the cached pair and as
	one half of the generate path."""
	pub = subprocess.run(
		["wg", "pubkey"], capture_output=True, check=True, text=True, input=private_b64 + "\n"
	)
	return pub.stdout.strip()


def _is_valid_private_b64(value: str) -> bool:
	"""Cheap sanity: a WireGuard private key is 32 bytes base64, so 44 bytes
	ending with one `=` padding char. The real validation is the round-trip
	through `wg pubkey` in `_existing_pair_valid`."""
	return (
		isinstance(value, str)
		and len(value) == 44
		and value.endswith("=")
		and all(c.isalnum() or c in "+/=" for c in value)
	)


def _existing_signing_pair_valid(priv_path: str, pub_path: str) -> bool:
	"""True iff both ed25519 key files exist AND the public verifies against
	the private — a tampered / interrupted first-boot pair is regenerated
	rather than trusted. Cheap check: try to sign a fixed nonce and verify;
	failure means a bad pair."""
	from pathlib import Path

	from . import signing

	if not Path(priv_path).exists() or not Path(pub_path).exists():
		return False
	priv_b64 = _read_key(priv_path)
	pub_b64 = _read_key(pub_path)
	test_dict = {"v": "self-test", "host_id": "self", "generation": 0}
	try:
		sig = signing.sign(test_dict, priv_b64, kind="membership")
		signing.verify({**test_dict, "signature": sig}, pub_b64, kind="membership")
		return True
	except Exception:
		return False


def _read_key(path: str) -> str:
	"""Read a key file (no extra whitespace expected, but strip defensively)."""
	return Path(path).read_text(encoding="utf-8").strip()


def _write_key(path: str, content: str, *, mode: int) -> None:
	"""Atomic write via a tempfile + `os.replace`, chmod the final path. Creates
	`/etc/atlas-networkd` if missing (0755, root-only reads inside; the keys
	inside are 0600/0644)."""
	p = Path(path)
	p.parent.mkdir(parents=True, exist_ok=True)
	tmp = p.with_suffix(p.suffix + ".tmp")
	tmp.write_text(content + "\n", encoding="utf-8")
	os.chmod(tmp, mode)
	os.replace(tmp, p)


__all__ = ["ensure_keypair"]
