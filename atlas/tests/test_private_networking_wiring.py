"""Controller-level wiring tests for the WireGuard host-mesh private plane.

The pure derivations live in `test_private_networking.py`; this module tests how
they are wired into the DocType controllers and Task variables (design §5, §6, §8) —
still with NO host, NO SSH, NO wg. Everything here runs under the bench suite in
milliseconds. It covers:

  - `Server` denormalizes its derived wg pubkey + mesh address on save (§8).
  - `Virtual Machine.set_private_address` denormalizes the derived /128 for a
    tenant VM, and leaves it empty for a tenant-less (operator-created) VM.
  - `_provision_variables` carries PRIVATE_ADDRESS + TENANT_PREFIX for a tenant VM
    and omits them for a tenant-less VM (so vm-network-up no-ops the private block).
  - a dark VM (public_networking=0) skips public /128 allocation and indexes its
    NAT44 /30 off the private address; an air-gapped VM (egress_nat44=0) emits no
    v4 link at all.
  - `Subdomain._denormalize_address` dials the public /128 for a public VM and the
    private /128 for a dark VM (§6, Phase 2).
  - the arbitrary-string tenant name (Central `Team.name`, a naming series, NOT a
    UUID) derives cleanly rather than crashing.
"""

import ipaddress

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.networking import (
	INFRA_PREFIX,
	derive_host_mesh_address,
	derive_host_wireguard_keypair,
	derive_private_address,
	derive_tenant_prefix,
)
from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine

TENANT_NAME = "TEAM-90001"  # a Central naming-series id — NOT a UUID, on purpose


def _ensure_tenant(name: str = TENANT_NAME) -> str:
	if not frappe.db.exists("Tenant", name):
		frappe.get_doc({"doctype": "Tenant", "team": name, "email": f"{name.lower()}@example.test"}).insert(
			ignore_permissions=True
		)
	return name


class TestServerMeshDenorm(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider("atlas-mesh-provider")

	def test_server_denormalizes_mesh_identity(self) -> None:
		server = make_server(self.provider, title="atlas-mesh-server")
		expected_key = derive_host_wireguard_keypair(server.name)[1]
		self.assertEqual(server.wireguard_public_key, expected_key)
		self.assertEqual(server.mesh_address, derive_host_mesh_address(server.name))

	def test_mesh_address_is_in_the_infra_48(self) -> None:
		server = make_server(self.provider, title="atlas-mesh-server-2")
		self.assertIn(ipaddress.IPv6Address(server.mesh_address), ipaddress.IPv6Network(INFRA_PREFIX))


class TestPrivateAddressDenorm(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider("atlas-priv-provider")
		self.server = make_server(
			self.provider,
			title="atlas-priv-server",
			status="Active",
			ipv6_address="2001:db8:aa::1",
			ipv6_prefix="2001:db8:aa::/64",
			ipv6_virtual_machine_range="2001:db8:aa::/124",
		)
		self.image = make_image("atlas-priv-image")

	def test_tenant_vm_denormalizes_private_address(self) -> None:
		tenant = _ensure_tenant()
		vm = make_virtual_machine(self.server, self.image, tenant=tenant)
		self.assertEqual(vm.private_address, derive_private_address(tenant, vm.name))

	def test_tenantless_vm_has_no_private_address(self) -> None:
		vm = make_virtual_machine(self.server, self.image)
		self.assertFalse(vm.private_address, "an operator VM with no tenant stays off the private plane")

	def test_private_address_survives_a_reload(self) -> None:
		tenant = _ensure_tenant()
		vm = make_virtual_machine(self.server, self.image, tenant=tenant)
		reloaded = frappe.get_doc("Virtual Machine", vm.name)
		self.assertEqual(reloaded.private_address, derive_private_address(tenant, vm.name))


class TestProvisionVariables(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider("atlas-provar-provider")
		self.server = make_server(
			self.provider,
			title="atlas-provar-server",
			status="Active",
			ipv6_address="2001:db8:bb::1",
			ipv6_prefix="2001:db8:bb::/64",
			ipv6_virtual_machine_range="2001:db8:bb::/124",
		)
		self.image = make_image("atlas-provar-image")

	def test_tenant_vm_carries_private_env(self) -> None:
		tenant = _ensure_tenant()
		vm = make_virtual_machine(self.server, self.image, tenant=tenant)
		variables = vm._provision_variables()
		self.assertEqual(variables["PRIVATE_ADDRESS"], derive_private_address(tenant, vm.name))
		self.assertEqual(variables["TENANT_PREFIX"], derive_tenant_prefix(tenant))

	def test_tenantless_vm_omits_private_env(self) -> None:
		vm = make_virtual_machine(self.server, self.image)
		variables = vm._provision_variables()
		self.assertNotIn("PRIVATE_ADDRESS", variables)
		self.assertNotIn("TENANT_PREFIX", variables)

	def test_private_address_is_inside_the_tenant_48(self) -> None:
		tenant = _ensure_tenant()
		vm = make_virtual_machine(self.server, self.image, tenant=tenant)
		prefix = ipaddress.IPv6Network(derive_tenant_prefix(tenant))
		self.assertIn(ipaddress.IPv6Address(vm.private_address), prefix)


class TestDarkVirtualMachine(IntegrationTestCase):
	"""public_networking=0 / egress_nat44=0 paths (§6). No host — just the controller's
	variable wiring for a VM that has no public /128."""

	def setUp(self) -> None:
		self.provider = make_provider("atlas-dark-provider")
		self.server = make_server(
			self.provider,
			title="atlas-dark-server",
			status="Active",
			ipv6_address="2001:db8:cc::1",
			ipv6_prefix="2001:db8:cc::/64",
			ipv6_virtual_machine_range="2001:db8:cc::/124",
		)
		self.image = make_image("atlas-dark-image")
		self.tenant = _ensure_tenant()

	def test_dark_vm_gets_no_public_ipv6(self) -> None:
		vm = make_virtual_machine(self.server, self.image, tenant=self.tenant, public_networking=0)
		self.assertFalse(vm.ipv6_address, "a dark VM consumes no public /124 slot")
		self.assertTrue(vm.private_address, "but it still gets its private identity")

	def test_tenantless_dark_vm_is_rejected(self) -> None:
		# A dark VM with no tenant would have NO identity at all (§6 invariant).
		with self.assertRaises(frappe.ValidationError):
			make_virtual_machine(self.server, self.image, public_networking=0)

	def test_dark_vm_indexes_v4_off_the_private_address(self) -> None:
		vm = make_virtual_machine(self.server, self.image, tenant=self.tenant, public_networking=0)
		variables = vm._provision_variables()
		# No public ipv6 to index off, so the /30 comes from the private /128's low bits.
		self.assertIn("IPV4_HOST_CIDR", variables)
		self.assertIn("IPV4_GUEST_CIDR", variables)

	def test_air_gapped_vm_emits_no_v4_link(self) -> None:
		vm = make_virtual_machine(
			self.server, self.image, tenant=self.tenant, public_networking=0, egress_nat44=0
		)
		variables = vm._provision_variables()
		self.assertNotIn("IPV4_HOST_CIDR", variables)
		self.assertNotIn("IPV4_GATEWAY", variables)
		# Still on the private plane (its only reachability).
		self.assertIn("PRIVATE_ADDRESS", variables)

	def test_public_vm_is_unchanged(self) -> None:
		vm = make_virtual_machine(self.server, self.image, tenant=self.tenant)
		self.assertTrue(vm.ipv6_address, "the default public VM still allocates a /128")
		variables = vm._provision_variables()
		self.assertIn("IPV4_HOST_CIDR", variables)


class TestSubdomainAddressSwitch(IntegrationTestCase):
	"""Subdomain._denormalize_address dials public xor private by public_networking (§6)."""

	def setUp(self) -> None:
		self.provider = make_provider("atlas-sub-provider")
		self.server = make_server(
			self.provider,
			title="atlas-sub-server",
			status="Active",
			ipv6_address="2001:db8:dd::1",
			ipv6_prefix="2001:db8:dd::/64",
			ipv6_virtual_machine_range="2001:db8:dd::/124",
		)
		self.image = make_image("atlas-sub-image")
		self.tenant = _ensure_tenant()

	def _make_subdomain(self, vm) -> frappe.model.document.Document:
		return frappe.get_doc(
			{
				"doctype": "Subdomain",
				"subdomain": frappe.generate_hash(length=8),
				"virtual_machine": vm.name,
				"status": "Active",
			}
		).insert(ignore_permissions=True)

	def test_public_vm_subdomain_dials_public_128(self) -> None:
		vm = make_virtual_machine(self.server, self.image, tenant=self.tenant)
		subdomain = self._make_subdomain(vm)
		self.assertEqual(subdomain.address, vm.ipv6_address)

	def test_dark_vm_subdomain_dials_private_128(self) -> None:
		vm = make_virtual_machine(self.server, self.image, tenant=self.tenant, public_networking=0)
		subdomain = self._make_subdomain(vm)
		self.assertEqual(subdomain.address, vm.private_address)
		self.assertTrue(subdomain.address.startswith("fdaa:"))


class TestTenantNameShapes(IntegrationTestCase):
	"""The Central Team id is a naming series (TEAM-#####), not a UUID — the derivation
	must handle it (and a raw UUID) without crashing. This is the regression guard for
	the uuid.UUID() crash that would otherwise hit every real create_vm/create_site."""

	def test_naming_series_tenant_derives(self) -> None:
		prefix = derive_tenant_prefix("TEAM-00042")
		self.assertTrue(prefix.startswith("fdaa:"))
		self.assertTrue(prefix.endswith("/48"))

	def test_uuid_and_series_names_are_distinct(self) -> None:
		import uuid

		a = derive_tenant_prefix("TEAM-00042")
		b = derive_tenant_prefix(str(uuid.uuid4()))
		self.assertNotEqual(a, b)

	def test_tenant_prefix_is_deterministic(self) -> None:
		self.assertEqual(derive_tenant_prefix("TEAM-00042"), derive_tenant_prefix("TEAM-00042"))
