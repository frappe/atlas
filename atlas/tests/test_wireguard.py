import base64
import shutil
import subprocess

from frappe.tests import IntegrationTestCase

from atlas.atlas.wireguard import (
	ENCODED_KEY_LENGTH,
	KEY_BYTES,
	is_valid_public_key,
)


class TestWireGuardKeys(IntegrationTestCase):
	def test_is_valid_public_key_accepts_a_real_wg_key(self):
		# A 32-byte standard-base64 value, the shape `wg pubkey` emits.
		key = base64.standard_b64encode(b"\x11" * KEY_BYTES).decode()
		self.assertEqual(len(key), ENCODED_KEY_LENGTH)
		self.assertTrue(is_valid_public_key(key))

	def test_is_valid_public_key_accepts_wg_tool_output(self):
		# Cross-check against WireGuard's own key generation, when the tool is on
		# the controller. Skips cleanly without it (still host-free — a local
		# subprocess, no remote host).
		if not shutil.which("wg"):
			self.skipTest("wg not installed on the controller")
		private = subprocess.run(["wg", "genkey"], capture_output=True, text=True, check=True).stdout.strip()
		public = subprocess.run(
			["wg", "pubkey"], input=private, capture_output=True, text=True, check=True
		).stdout.strip()
		self.assertTrue(is_valid_public_key(public))

	def test_is_valid_public_key_rejects_malformed(self):
		# Wrong length (short / long), a base64 value of the wrong byte count, and
		# a 44-char string with a character outside the base64 alphabet.
		short = base64.standard_b64encode(b"\x00" * 16).decode()  # 24 chars
		for bad in ("", "not a key", "x" * 43, "x" * 45, short, "!" + "A" * 43):
			self.assertFalse(is_valid_public_key(bad), bad)

	def test_is_valid_public_key_rejects_non_string(self):
		# Defensive: the API boundary may hand us a non-str; the isinstance guard
		# rejects it rather than raising.
		for bad in (None, 1234):
			self.assertFalse(is_valid_public_key(bad))  # type: ignore[arg-type]
