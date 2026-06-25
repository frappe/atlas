"""SSH Key DocType: validation, fingerprint derivation.

SSH Key is an operator/Central-facing DocType (spec/02 § SSH Key). A key is
registered once and its body copied into a VM's immutable `ssh_public_key` at
provision. These tests pin:

1. `validate()` derives the standard `SHA256:<base64nopad>` fingerprint.
2. A malformed key fails loud at the boundary (Taste 17) — not stored to fail
   opaquely at provision time.
"""

import base64
import hashlib

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.ssh_key.ssh_key import fingerprint

# A complete, valid ed25519 public key (valid base64 body, padded).
VALID_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDLM3M2qZ8mLkUo6L1l0wq3rT7kqQ0jJ8wKf5cN0pQaX laptop@example"


def _expected_fingerprint(public_key: str) -> str:
	blob = public_key.split()[1]
	raw = base64.b64decode(blob)
	digest = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
	return f"SHA256:{digest}"


# Shared test-user helpers. The end-user `Atlas User` role + owner-scoping were
# retired from the app (Central is the front door), but the firewall + VPN-broker
# tests still need a non-operator (non-System-Manager) user that *owns* a VM to
# exercise owner-scoped access — so these recreate that role as a test fixture.
def _ensure_atlas_user_role() -> None:
	if not frappe.db.exists("Role", "Atlas User"):
		frappe.get_doc({"doctype": "Role", "role_name": "Atlas User", "desk_access": 0}).insert(
			ignore_permissions=True
		)


def _make_user(email: str, *, role: str | None) -> str:
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "SSH",
				"last_name": "Key",
				"send_welcome_email": 0,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	for role_row in list(user.get("roles") or []):
		user.remove(role_row)
	if role:
		user.append("roles", {"role": role})
	user.save(ignore_permissions=True)
	return user.name


class TestSSHKeyValidation(IntegrationTestCase):
	def test_pure_fingerprint_helper(self) -> None:
		self.assertEqual(fingerprint(VALID_KEY), _expected_fingerprint(VALID_KEY))
		self.assertTrue(fingerprint(VALID_KEY).startswith("SHA256:"))

	def test_validate_derives_fingerprint_on_insert(self) -> None:
		doc = frappe.get_doc({"doctype": "SSH Key", "key_name": "laptop", "public_key": VALID_KEY}).insert(
			ignore_permissions=True
		)
		self.assertEqual(doc.fingerprint, _expected_fingerprint(VALID_KEY))

	def test_whitespace_is_stripped(self) -> None:
		doc = frappe.get_doc(
			{
				"doctype": "SSH Key",
				"key_name": "padded",
				"public_key": f"  \n{VALID_KEY}\n  ",
			}
		).insert(ignore_permissions=True)
		self.assertEqual(doc.public_key, VALID_KEY)
		self.assertEqual(doc.fingerprint, _expected_fingerprint(VALID_KEY))

	def test_unknown_type_rejected(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{"doctype": "SSH Key", "key_name": "bad", "public_key": "not-a-key AAAA x"}
			).insert(ignore_permissions=True)

	def test_missing_blob_rejected(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "SSH Key", "key_name": "bad", "public_key": "ssh-ed25519"}).insert(
				ignore_permissions=True
			)

	def test_bad_base64_rejected(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "SSH Key",
					"key_name": "bad",
					"public_key": "ssh-ed25519 not!valid!base64!!! x@y",
				}
			).insert(ignore_permissions=True)
