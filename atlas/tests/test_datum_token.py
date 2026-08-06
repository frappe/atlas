import importlib.util
import time
import unittest

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_MODULE_PATH = "/home/qwerty/worktrees/atlas-datum-metrics/atlas/atlas/datum_token.py"


def _load_module():
	spec = importlib.util.spec_from_file_location("datum_token_under_test", _MODULE_PATH)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _keypair():
	key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
	private_pem = key.private_bytes(
		serialization.Encoding.PEM,
		serialization.PrivateFormat.PKCS8,
		serialization.NoEncryption(),
	).decode()
	public_pem = key.public_key().public_bytes(
		serialization.Encoding.PEM,
		serialization.PublicFormat.SubjectPublicKeyInfo,
	).decode()
	return private_pem, public_pem


class TestDatumToken(unittest.TestCase):
	def test_encode_token_verifies_and_carries_claims(self):
		module = _load_module()
		private_pem, public_pem = _keypair()
		token = module.encode_token("vm-abc123", private_pem, ttl_seconds=3600, key_id="k1")

		claims = jwt.decode(token, public_pem, algorithms=["RS256"])
		self.assertEqual(claims["resource_id"], "vm-abc123")
		self.assertIn("write", claims["access"])
		self.assertGreater(claims["exp"], int(time.time()))
		header = jwt.get_unverified_header(token)
		self.assertEqual(header["kid"], "k1")

	def test_build_bundle_has_host_and_vm_tokens(self):
		module = _load_module()
		private_pem, public_pem = _keypair()
		bundle = module.build_bundle("atlas-host-1", ["vm-a", "vm-b"], private_pem, key_id="k1")
		import jwt

		self.assertEqual(jwt.decode(bundle["host"], public_pem, algorithms=["RS256"])["resource_id"], "atlas-host-1")
		self.assertEqual(set(bundle["vms"]), {"vm-a", "vm-b"})
		self.assertEqual(jwt.decode(bundle["vms"]["vm-a"], public_pem, algorithms=["RS256"])["resource_id"], "vm-a")

	def test_wrong_key_is_rejected(self):
		module = _load_module()
		private_pem, _ = _keypair()
		_, other_public = _keypair()
		token = module.encode_token("server-1", private_pem)
		with self.assertRaises(jwt.InvalidSignatureError):
			jwt.decode(token, other_public, algorithms=["RS256"])


if __name__ == "__main__":
	unittest.main()
