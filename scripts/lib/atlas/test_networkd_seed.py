"""Unit tests for the bootstrap seed loader's operator-signature verification
(spec §9.2 / §19.4 — the seed is the SOLE trust root, so its operator signature
is a hard load-time MUST: a bad/missing signature is a fail-closed bootstrap
failure; only a cluster with NO operator key configured loads unverified, with
a loud stderr warning, to keep dev/test bring-up working).

`cryptography` is already a host dep (`scripts/host-pyproject.toml`); run under
`python3.14 -m unittest` in an env that has it. A throwaway operator keypair is
generated in-test via `signing.generate_keypair_raw`.
"""

import base64
import json
import tempfile
import unittest
from pathlib import Path

from atlas.networkd.seed import SEED_SIG_SUFFIX, load_seed, load_seed_optional
from atlas.networkd.signing import SignatureError, generate_keypair_raw, sign_detached

# A well-formed seed entry shape (endpoint / mesh_address pass MembershipRecord.validate).
_ENTRIES = [
	{
		"host_id": "A",
		"endpoint": "2001:db9::a",
		"wg_public_key": "wKA",
		"signing_public_key": "sKA",
		"mesh_address": "fdaa:0:0:a::1",
		"generation": 1,
	},
]


def _operator_keypair():
	priv_raw, pub_raw = generate_keypair_raw()
	return base64.b64encode(priv_raw).decode(), base64.b64encode(pub_raw).decode()


def _write_seed_dir(d: Path, *, operator_pub: str | None, sign_with: str | None, tamper_sig: bool = False):
	"""Write seed.json (+ optional operator-public-key and seed.json.sig) into
	dir `d`. `sign_with` is the operator priv b64 to sign the EXACT seed bytes
	with, or None to skip the signature file. Returns the seed.json path."""
	seed_path = d / "seed.json"
	content = json.dumps(_ENTRIES, sort_keys=True) + "\n"
	seed_path.write_text(content, encoding="utf-8")
	if operator_pub is not None:
		(d / "operator-public-key").write_text(operator_pub + "\n", encoding="utf-8")
	if sign_with is not None:
		sig = sign_detached(content.encode("utf-8"), sign_with)
		if tamper_sig:
			# Flip the signature to a DIFFERENT valid-shaped signature (sign
			# tampered bytes) so it's well-formed base64 but doesn't verify.
			sig = sign_detached((content + "TAMPER").encode("utf-8"), sign_with)
		(d / (seed_path.name + SEED_SIG_SUFFIX)).write_text(sig + "\n", encoding="utf-8")
	return seed_path


class TestSeedSignatureVerification(unittest.TestCase):
	def test_validly_signed_seed_loads(self):
		"""(a) An operator pubkey is configured and the signature over the exact
		seed bytes verifies — the records load."""
		priv, pub = _operator_keypair()
		with tempfile.TemporaryDirectory() as d:
			seed_path = _write_seed_dir(Path(d), operator_pub=pub, sign_with=priv)
			records = load_seed(str(seed_path))
			self.assertEqual([r.host_id for r in records], ["A"])
			self.assertEqual(records[0].signing_public_key, "sKA")

	def test_tampered_signature_raises_and_installs_nothing(self):
		"""(b) A configured operator pubkey + a BAD/tampered signature => hard
		failure, no records. Fail closed."""
		priv, pub = _operator_keypair()
		with tempfile.TemporaryDirectory() as d:
			seed_path = _write_seed_dir(Path(d), operator_pub=pub, sign_with=priv, tamper_sig=True)
			with self.assertRaises(SignatureError):
				load_seed(str(seed_path))

	def test_wrong_operator_key_raises(self):
		"""A signature made by a DIFFERENT operator key than the configured
		pubkey must not verify."""
		priv_signer, _pub_signer = _operator_keypair()
		_priv_other, pub_configured = _operator_keypair()
		with tempfile.TemporaryDirectory() as d:
			seed_path = _write_seed_dir(Path(d), operator_pub=pub_configured, sign_with=priv_signer)
			with self.assertRaises(SignatureError):
				load_seed(str(seed_path))

	def test_no_signature_but_operator_key_configured_raises(self):
		"""(c) A configured operator pubkey + NO signature file => hard failure.
		An unsigned trust root is never trusted in production."""
		_priv, pub = _operator_keypair()
		with tempfile.TemporaryDirectory() as d:
			seed_path = _write_seed_dir(Path(d), operator_pub=pub, sign_with=None)
			with self.assertRaises(SignatureError):
				load_seed(str(seed_path))

	def test_no_operator_key_loads_unverified_with_warning(self):
		"""(d) The dev/test posture — NO operator pubkey configured => load
		unverified, but emit a loud stderr warning about the unverified trust
		root. Bring-up isn't blocked; production (with a key) still fails closed."""
		import contextlib
		import io

		with tempfile.TemporaryDirectory() as d:
			# No operator-public-key file, no signature file.
			seed_path = _write_seed_dir(Path(d), operator_pub=None, sign_with=None)
			stderr = io.StringIO()
			with contextlib.redirect_stderr(stderr):
				records = load_seed(str(seed_path))
			self.assertEqual([r.host_id for r in records], ["A"])
			self.assertIn("UNVERIFIED", stderr.getvalue())

	def test_no_operator_key_ignores_stray_signature(self):
		"""With no operator pubkey, a present (even bogus) signature file is
		irrelevant — the warning path loads unverified regardless."""
		import contextlib
		import io

		priv, _pub = _operator_keypair()
		with tempfile.TemporaryDirectory() as d:
			# Signature present but NO operator-public-key => still the dev path.
			seed_path = _write_seed_dir(Path(d), operator_pub=None, sign_with=priv)
			stderr = io.StringIO()
			with contextlib.redirect_stderr(stderr):
				records = load_seed(str(seed_path))
			self.assertEqual([r.host_id for r in records], ["A"])
			self.assertIn("UNVERIFIED", stderr.getvalue())

	def test_load_seed_optional_verifies_when_present(self):
		"""`load_seed_optional` inherits verification for a present file — a
		configured pubkey with a bad signature still fails closed."""
		priv, pub = _operator_keypair()
		with tempfile.TemporaryDirectory() as d:
			seed_path = _write_seed_dir(Path(d), operator_pub=pub, sign_with=priv, tamper_sig=True)
			with self.assertRaises(SignatureError):
				load_seed_optional(str(seed_path))

	def test_load_seed_optional_absent_returns_empty(self):
		"""An absent seed file is the come-up-peer-empty posture — [] and no
		verification."""
		with tempfile.TemporaryDirectory() as d:
			self.assertEqual(load_seed_optional(str(Path(d) / "seed.json")), [])


if __name__ == "__main__":
	unittest.main()
