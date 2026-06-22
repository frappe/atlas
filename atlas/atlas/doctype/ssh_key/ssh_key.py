"""SSH Key — a public key a dashboard user registers once and chooses when
creating a Virtual Machine.

A per-user owned DocType (Frappe's built-in `owner`, scoped by the `if_owner`
permission row and `permissions.owner_only`), mirroring Virtual Machine. It is
pure data: no Tasks, no lifecycle methods. The dashboard SPA reads the user's
keys and, on machine create, copies the chosen key's `public_key` body into the
VM's immutable `ssh_public_key` field — so this DocType adds nothing to the
provisioning path.

`fingerprint` is derived from `public_key` on save (the standard
`SHA256:<base64nopad>` form `ssh-keygen -lf` prints) so the SPA can show a
recognizable key identity without echoing the whole blob.
"""

import base64
import binascii
import hashlib

import frappe
from frappe import _
from frappe.model.document import Document

# OpenSSH public-key type prefixes we accept. Covers the modern defaults
# (ed25519, rsa, ecdsa) and FIDO/U2F security keys (sk-*). A key whose first
# token isn't one of these is rejected at the boundary rather than stored and
# silently failing to inject at provision time.
KNOWN_KEY_TYPES = (
	"ssh-ed25519",
	"ssh-rsa",
	"ssh-dss",
	"ecdsa-sha2-nistp256",
	"ecdsa-sha2-nistp384",
	"ecdsa-sha2-nistp521",
	"sk-ssh-ed25519@openssh.com",
	"sk-ecdsa-sha2-nistp256@openssh.com",
)


class SSHKey(Document):
	def validate(self) -> None:
		self.public_key = (self.public_key or "").strip()
		self.fingerprint = fingerprint(self.public_key)


def fingerprint(public_key: str) -> str:
	"""SHA-256 fingerprint of an OpenSSH public key, `SHA256:<base64nopad>`.

	The wire form `ssh-keygen -lf` prints. An OpenSSH public key is
	`<type> <base64-blob> [comment]`; the fingerprint is the SHA-256 of the
	decoded blob, base64-encoded without padding. Fails loud (Taste 17) on a
	missing type token, an unknown type, or an unparseable blob — a malformed
	key stored now is an opaque SSH failure at provision time later."""
	parts = (public_key or "").split()
	if len(parts) < 2:
		frappe.throw(_("Not an OpenSSH public key: expected '<type> <base64-key> [comment]'."))
	key_type, blob = parts[0], parts[1]
	if key_type not in KNOWN_KEY_TYPES:
		frappe.throw(f"Unsupported SSH key type {key_type!r}.")
	try:
		raw = base64.b64decode(blob, validate=True)
	except binascii.Error, ValueError:
		frappe.throw(_("SSH key body is not valid base64."))
	digest = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
	return f"SHA256:{digest}"
