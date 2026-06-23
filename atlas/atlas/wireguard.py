"""WireGuard key validation at the controller boundary.

The VPN broker (spec/19-vpn-broker.md) terminates each tunnel on the host, and
the **host** mints its own keypair (like a guest's SSH host keys — host-resident
crypto identity, never routed through a Task and never stored in the Frappe DB).
The client likewise mints its own keypair and sends only its public key.

So the controller never generates or holds a private key; its only key concern is
to reject a malformed *client* public key before a Task is dispatched, which
`is_valid_public_key` does. Pure and host-free.
"""

from __future__ import annotations

import base64

# A WireGuard key is a 32-byte value rendered in standard base64 — 44 chars,
# '='-padded, exactly as `wg genkey` / `wg pubkey` emit it.
KEY_BYTES = 32
ENCODED_KEY_LENGTH = 44


def is_valid_public_key(key: str) -> bool:
	"""True iff `key` is a syntactically valid WireGuard public key — a 32-byte
	value in standard base64. Guards the client-supplied public key at the API
	boundary so a malformed key fails loud in the controller, not on the host."""
	if not isinstance(key, str) or len(key) != ENCODED_KEY_LENGTH:
		return False
	try:
		# validate=True rejects non-alphabet characters rather than silently
		# discarding them (which could let a 44-char junk string decode short).
		return len(base64.b64decode(key, validate=True)) == KEY_BYTES
	except ValueError:
		# binascii.Error (bad base64) is a ValueError subclass.
		return False
