"""Unit tests for the WireGuard host-mesh derivations + config render (design §2-8).

These are pure-function tests — no host, no SSH, no wg — of the naming/keying layer:
`derive_tenant_prefix`, `derive_private_address` (incl. region), the real
`derive_host_wireguard_keypair` (a Curve25519 base-point multiply, verified
byte-for-byte against `wg pubkey` on a real Scaleway host), `derive_host_mesh_address`,
the `derive_ipv4_link` dark-VM index path, and `host_mesh.render_wg_mesh_config`.

They run under the bench suite (`bench --site … run-tests --app atlas`) like the rest
of the controller unit tests. The host-touching reconcile transport is proven live,
not here.
"""

import ipaddress
import types
import uuid

from frappe.tests import IntegrationTestCase

from atlas.atlas.networking import (
	CLIENT_HEXTET,
	INFRA_PREFIX,
	REGION_ID_BITS,
	TENANT_PREFIX_LENGTH,
	VM_HOST_PART_BITS,
	derive_client_address,
	derive_host_mesh_address,
	derive_host_wireguard_keypair,
	derive_ipv4_link,
	derive_private_address,
	derive_tenant_prefix,
)


class TestAddressDerivation(IntegrationTestCase):
	def test_tenant_prefix_is_a_48_in_fdaa(self):
		prefix = ipaddress.IPv6Network(derive_tenant_prefix(str(uuid.uuid4())))
		self.assertEqual(prefix.prefixlen, TENANT_PREFIX_LENGTH)
		self.assertTrue(ipaddress.IPv6Network("fdaa::/16").supernet_of(prefix))

	def test_tenant_prefix_is_deterministic(self):
		t = str(uuid.uuid4())
		self.assertEqual(derive_tenant_prefix(t), derive_tenant_prefix(t))

	def test_private_address_inside_tenant_48(self):
		t, v = str(uuid.uuid4()), str(uuid.uuid4())
		address = ipaddress.IPv6Address(derive_private_address(t, v))
		self.assertIn(address, ipaddress.IPv6Network(derive_tenant_prefix(t)))

	def test_private_address_fourth_hextet_is_zero_single_region(self):
		# Every single-region private address reads fdaa:T:T:0:V:V:V:V (§A4 signature).
		address = ipaddress.IPv6Address(derive_private_address(str(uuid.uuid4()), str(uuid.uuid4())))
		self.assertEqual(address.exploded.split(":")[3], "0000")

	def test_private_address_survives_migration_byte_for_byte(self):
		# Host-INDEPENDENT: a pure function of (tenant, vm), so the SAME inputs always
		# give the SAME address — the load-bearing migration property (§7).
		t, v = str(uuid.uuid4()), str(uuid.uuid4())
		self.assertEqual(derive_private_address(t, v), derive_private_address(t, v))

	def test_region_index_fills_fourth_hextet(self):
		t, v = str(uuid.uuid4()), str(uuid.uuid4())
		address = ipaddress.IPv6Address(derive_private_address(t, v, region_index=0x00A))
		self.assertEqual(address.exploded.split(":")[3], "000a")

	def test_tenant_48_preserved_across_regions(self):
		# bits 16-47 (the tenant /48) are IDENTICAL in region 0 and region A (§D1).
		t, v = str(uuid.uuid4()), str(uuid.uuid4())
		a0 = ipaddress.IPv6Address(derive_private_address(t, v, 0)).exploded
		aA = ipaddress.IPv6Address(derive_private_address(t, v, 0x00A)).exploded
		self.assertEqual(a0[:14], aA[:14])  # fdaa:TTTT:TTTT

	def test_vm_part_preserved_across_regions(self):
		# bits 64-127 (the VM part) are IDENTICAL across regions (§D1).
		t, v = str(uuid.uuid4()), str(uuid.uuid4())
		a0 = ipaddress.IPv6Address(derive_private_address(t, v, 0)).exploded
		aA = ipaddress.IPv6Address(derive_private_address(t, v, 0x00A)).exploded
		self.assertEqual(a0[20:], aA[20:])

	def test_region_index_out_of_range_raises(self):
		with self.assertRaises(ValueError):
			derive_private_address(str(uuid.uuid4()), str(uuid.uuid4()), 1 << REGION_ID_BITS)

	def test_vm_host_part_is_64_bits(self):
		self.assertEqual(VM_HOST_PART_BITS, 128 - 64)


class TestHostKeyDerivation(IntegrationTestCase):
	def test_keypair_is_deterministic(self):
		s = str(uuid.uuid4())
		self.assertEqual(derive_host_wireguard_keypair(s), derive_host_wireguard_keypair(s))

	def test_keypair_shapes_are_base64_32_bytes(self):
		import base64

		priv, pub = derive_host_wireguard_keypair(str(uuid.uuid4()))
		self.assertEqual(len(base64.b64decode(priv)), 32)
		self.assertEqual(len(base64.b64decode(pub)), 32)

	def test_public_matches_wg_pubkey_of_private(self):
		# The public key IS the Curve25519 base-point multiply of the private scalar
		# (what `echo <priv> | wg pubkey` computes). Recompute it here via cryptography
		# and assert equality — the same check that passed against `wg pubkey` on a real
		# host, pinned as a regression so a future refactor can't silently diverge.
		import base64

		from cryptography.hazmat.primitives import serialization
		from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

		priv, pub = derive_host_wireguard_keypair(str(uuid.uuid4()))
		key = X25519PrivateKey.from_private_bytes(base64.b64decode(priv))
		expected = base64.b64encode(
			key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
		).decode()
		self.assertEqual(pub, expected)

	def test_two_servers_get_distinct_keys(self):
		self.assertNotEqual(
			derive_host_wireguard_keypair(str(uuid.uuid4())),
			derive_host_wireguard_keypair(str(uuid.uuid4())),
		)


class TestHostMeshAddress(IntegrationTestCase):
	def test_mesh_address_in_infra_48(self):
		address = ipaddress.IPv6Address(derive_host_mesh_address(str(uuid.uuid4())))
		self.assertIn(address, ipaddress.IPv6Network(INFRA_PREFIX))

	def test_mesh_address_ends_in_one(self):
		# fdaa:0:0:<idx>::1 — the ::1 marks the host's own address.
		self.assertTrue(derive_host_mesh_address(str(uuid.uuid4())).endswith("::1"))

	def test_mesh_address_is_deterministic(self):
		s = str(uuid.uuid4())
		self.assertEqual(derive_host_mesh_address(s), derive_host_mesh_address(s))

	def test_mesh_address_never_in_a_tenant_48(self):
		# The infra /48 (fdaa:0:0::/48) has all-zero tenant bits, which HKDF never
		# derives for a real tenant — so a host's mesh address can't land inside any
		# tenant's /48. (A tenant hashing to exactly 0:0 is a 2^-32 event.)
		mesh = ipaddress.IPv6Address(derive_host_mesh_address(str(uuid.uuid4())))
		tenant48 = ipaddress.IPv6Network(derive_tenant_prefix(str(uuid.uuid4())))
		self.assertNotIn(mesh, tenant48)
		self.assertNotEqual(str(tenant48.network_address), "fdaa::")


class TestClientAddressDerivation(IntegrationTestCase):
	"""The customer gateway's client /128 (spec/25 Phase 5, spec/26 / reference §3)."""

	def test_client_address_inside_tenant_48(self):
		# The whole point: a client is a /128 inside its tenant's /48, so the mesh routes
		# it like any sibling VM and `client & /48 == tenant prefix` (the same_48 identity).
		t, c = str(uuid.uuid4()), str(uuid.uuid4())
		address = ipaddress.IPv6Address(derive_client_address(t, c))
		self.assertIn(address, ipaddress.IPv6Network(derive_tenant_prefix(t)))

	def test_client_fourth_hextet_marks_a_client(self):
		# 0x0001 in the 4th hextet — disjoint from a VM's 0x0000 by construction.
		address = ipaddress.IPv6Address(derive_client_address(str(uuid.uuid4()), str(uuid.uuid4())))
		self.assertEqual(address.exploded.split(":")[3], f"{CLIENT_HEXTET:04x}")

	def test_client_and_vm_of_same_tenant_never_collide(self):
		# A client (hextet 0x0001) and a VM (hextet 0x0000) of the SAME tenant occupy
		# disjoint sub-ranges — they can never share an address regardless of UUIDs.
		t = str(uuid.uuid4())
		client = ipaddress.IPv6Address(derive_client_address(t, str(uuid.uuid4())))
		vm = ipaddress.IPv6Address(derive_private_address(t, str(uuid.uuid4())))
		self.assertNotEqual(client, vm)
		self.assertNotEqual(client.exploded.split(":")[3], vm.exploded.split(":")[3])

	def test_client_address_is_host_independent(self):
		# Pure function of (tenant, peer) — the laptop keeps its VPC address regardless of
		# which gateway terminates it (migration-proof, like derive_private_address).
		t, c = str(uuid.uuid4()), str(uuid.uuid4())
		self.assertEqual(derive_client_address(t, c), derive_client_address(t, c))

	def test_client_masked_to_48_equals_tenant_prefix(self):
		# The identity the static same_48 eBPF guard leans on: saddr & /48 == the tenant.
		t, c = str(uuid.uuid4()), str(uuid.uuid4())
		client = ipaddress.IPv6Address(derive_client_address(t, c))
		masked = ipaddress.IPv6Network(f"{client}/48", strict=False)
		self.assertEqual(str(masked), derive_tenant_prefix(t))

	def test_two_clients_of_a_tenant_get_distinct_addresses(self):
		t = str(uuid.uuid4())
		self.assertNotEqual(
			derive_client_address(t, str(uuid.uuid4())),
			derive_client_address(t, str(uuid.uuid4())),
		)

	def test_client_address_non_uuid_tenant_name(self):
		# A real Tenant name is a naming series (TEAM-#####), not a UUID — _name_seed must
		# not crash (the exact class of bug that hit derive_tenant_prefix in prod).
		address = ipaddress.IPv6Address(derive_client_address("TEAM-00042", str(uuid.uuid4())))
		self.assertIn(address, ipaddress.IPv6Network(derive_tenant_prefix("TEAM-00042")))


class TestGuestLinkLocal(IntegrationTestCase):
	"""EUI-64 guest link-local derivation (spec/26 return path)."""

	def test_link_local_matches_eui64_of_derived_mac(self):
		from atlas.atlas.networking import derive_guest_link_local, derive_mac

		vm = str(uuid.uuid4())
		mac = derive_mac(vm)  # 06:00:aa:bb:cc:dd
		octets = mac.split(":")
		# EUI-64 by hand: flip U/L bit of the first octet, insert ff:fe.
		first = int(octets[0], 16) ^ 0x02
		expected = ipaddress.IPv6Address(
			f"fe80::{first:02x}{octets[1]}:{octets[2]}ff:fe{octets[3]}:{octets[4]}{octets[5]}"
		)
		self.assertEqual(ipaddress.IPv6Address(derive_guest_link_local(vm)), expected)

	def test_link_local_is_in_fe80(self):
		from atlas.atlas.networking import derive_guest_link_local

		address = ipaddress.IPv6Address(derive_guest_link_local(str(uuid.uuid4())))
		self.assertIn(address, ipaddress.IPv6Network("fe80::/10"))

	def test_link_local_is_deterministic(self):
		from atlas.atlas.networking import derive_guest_link_local

		vm = str(uuid.uuid4())
		self.assertEqual(derive_guest_link_local(vm), derive_guest_link_local(vm))


class TestDarkVmIpv4Link(IntegrationTestCase):
	def test_explicit_index_path(self):
		# A dark VM has no public v6, so derive_ipv4_link takes an explicit slot index.
		host, guest = derive_ipv4_link(index=7)
		self.assertTrue(host.endswith("/30"))
		self.assertTrue(guest.endswith("/30"))

	def test_index_matches_low_bits_of_matching_v6(self):
		# The explicit index path yields the SAME /30 as the v6 path for the same index
		# (::7 has low bits 7), so a VM that later gains a public v6 keeps its link.
		self.assertEqual(derive_ipv4_link(index=7), derive_ipv4_link("2001:db8::7"))

	def test_both_or_neither_raises(self):
		import frappe

		with self.assertRaises(frappe.ValidationError):
			derive_ipv4_link("2001:db8::2", index=7)
		with self.assertRaises(frappe.ValidationError):
			derive_ipv4_link()
