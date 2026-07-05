"""Controller-level wiring tests for the customer gateway (spec/25 Phase 5, spec/26).

The pure `derive_client_address` derivation lives in `test_private_networking.py`; this
module tests how the gateway is wired into the DocType controllers, the reconcile render,
and the host-mesh client-/128 fold — with NO host, NO SSH, NO wg. Everything runs under
the bench suite in milliseconds. It covers:

  - `VPN Peer` denormalizes client_address / allowed_ips / endpoint on insert,
    and freezes tenant + client_public_key after insert.
  - a malformed client public key and a missing gateway fail loud in before_insert.
  - `resolve_region_gateway` returns the one is_gateway VM (errors on none / >1).
  - `render_wg0_config` emits one source-pinned [Peer] per Active peer, sorted, and
    drops Revoked peers (the reconcile "in sync" byte shape).
  - `_add_customer_vpc_clients` folds an Active peer's /128 into the gateway host's mesh
    AllowedIPs (return path), and withdraws it when Revoked.
  - a VM cannot be both is_proxy and is_gateway.
"""

import ipaddress

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import customer_gateway
from atlas.atlas.host_mesh import _residents_by_host
from atlas.atlas.networking import CLIENT_HEXTET, derive_client_address, derive_tenant_prefix
from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine

TENANT_NAME = "TEAM-95001"  # a Central naming-series id — NOT a UUID, on purpose
# A syntactically valid WireGuard public key (32 bytes base64) for the controller guard.
VALID_KEY = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="


def _ensure_tenant(name: str = TENANT_NAME) -> str:
	if not frappe.db.exists("Tenant", name):
		frappe.get_doc({"doctype": "Tenant", "team": name, "email": f"{name.lower()}@example.test"}).insert(
			ignore_permissions=True
		)
	return name


class _GatewayFixture(IntegrationTestCase):
	"""Shared setup: a provider, an Active server, an image, and one is_gateway VM with a
	public IPv4 so a peer's endpoint denorm resolves."""

	def setUp(self) -> None:
		# resolve_region_gateway expects exactly one is_gateway VM region-wide. Other
		# suites (or a prior run whose commit escaped rollback) may leave stray gateways,
		# so demote any existing ones to keep this suite's expectations deterministic.
		for name in frappe.get_all("Virtual Machine", filters={"is_gateway": 1}, pluck="name"):
			frappe.db.set_value("Virtual Machine", name, "is_gateway", 0)
		self.tenant = _ensure_tenant()
		self.provider = make_provider("atlas-gw-provider")
		self.server = make_server(
			self.provider,
			title="atlas-gw-server",
			status="Active",
			ipv6_address="2001:db8:bb::1",
			ipv6_prefix="2001:db8:bb::/64",
			ipv6_virtual_machine_range="2001:db8:bb::/124",
		)
		self.image = make_image("atlas-gw-image")
		self.gateway = make_virtual_machine(
			self.server,
			self.image,
			title="atlas-gateway",
			status="Running",
			is_gateway=1,
			public_ipv4="203.0.113.9",
		)

	def _make_peer(self, label="alice-laptop", key=VALID_KEY, **overrides):
		doc = {
			"doctype": "VPN Peer",
			"tenant": self.tenant,
			"label": label,
			"client_public_key": key,
		}
		doc.update(overrides)
		return frappe.get_doc(doc).insert(ignore_permissions=True)


class TestVPNPeerDenorm(_GatewayFixture):
	def test_peer_denormalizes_computed_fields(self) -> None:
		peer = self._make_peer()
		self.assertEqual(peer.client_address, derive_client_address(self.tenant, peer.name))
		self.assertEqual(peer.allowed_ips, derive_tenant_prefix(self.tenant))
		self.assertEqual(peer.endpoint, "203.0.113.9:51820")
		# The client /128 is inside the tenant /48 with the 0x0001 client marker.
		address = ipaddress.IPv6Address(peer.client_address)
		self.assertIn(address, ipaddress.IPv6Network(derive_tenant_prefix(self.tenant)))
		self.assertEqual(address.exploded.split(":")[3], f"{CLIENT_HEXTET:04x}")

	def test_peer_resolves_region_gateway_on_insert(self) -> None:
		peer = self._make_peer()
		self.assertEqual(peer.gateway, self.gateway.name)

	def test_tenant_and_key_immutable_after_insert(self) -> None:
		peer = self._make_peer()
		peer.client_public_key = "aTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
		with self.assertRaises(frappe.ValidationError):
			peer.save(ignore_permissions=True)

	def test_malformed_client_key_fails_in_controller(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			self._make_peer(key="not-a-valid-wireguard-key")


class TestResolveRegionGateway(_GatewayFixture):
	def test_resolves_the_one_gateway(self) -> None:
		self.assertEqual(customer_gateway.resolve_region_gateway(), self.gateway.name)

	def test_errors_when_no_gateway(self) -> None:
		self.gateway.db_set("is_gateway", 0)
		with self.assertRaises(frappe.ValidationError):
			customer_gateway.resolve_region_gateway()

	def test_errors_when_more_than_one_gateway(self) -> None:
		make_virtual_machine(self.server, self.image, title="atlas-gateway-2", status="Running", is_gateway=1)
		with self.assertRaises(frappe.ValidationError):
			customer_gateway.resolve_region_gateway()


class TestRenderWg0Config(_GatewayFixture):
	def test_renders_source_pinned_peer(self) -> None:
		peer = self._make_peer()
		peer.db_set("status", "Active")
		config = customer_gateway.render_wg0_config(self.gateway.name)
		self.assertIn(f"PublicKey = {VALID_KEY}", config)
		# AllowedIPs is the client's OWN /128 — the source pin, not the tenant /48.
		self.assertIn(f"AllowedIPs = {peer.client_address}/128", config)

	def test_revoked_peer_is_dropped(self) -> None:
		peer = self._make_peer()
		peer.db_set("status", "Revoked")
		config = customer_gateway.render_wg0_config(self.gateway.name)
		self.assertNotIn(VALID_KEY, config)

	def test_peers_sorted_by_public_key(self) -> None:
		key_a = "aTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
		key_z = "zTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
		self._make_peer(label="z", key=key_z).db_set("status", "Active")
		self._make_peer(label="a", key=key_a).db_set("status", "Active")
		config = customer_gateway.render_wg0_config(self.gateway.name)
		self.assertLess(config.index(key_a), config.index(key_z))

	def test_empty_gateway_renders_no_peers(self) -> None:
		# A fully-revoked / never-enrolled gateway renders an empty body — wg syncconf
		# reads it as "no peers", correctly draining the interface.
		config = customer_gateway.render_wg0_config(self.gateway.name)
		self.assertNotIn("[Peer]", config)


class TestHostMeshClientFold(_GatewayFixture):
	def test_active_peer_client_128_joins_gateway_host_allowedips(self) -> None:
		peer = self._make_peer()
		peer.db_set("status", "Active")
		residents = _residents_by_host([{"name": self.server.name}])
		self.assertIn(peer.client_address, residents[self.server.name])

	def test_revoked_peer_client_128_withdrawn(self) -> None:
		peer = self._make_peer()
		peer.db_set("status", "Revoked")
		residents = _residents_by_host([{"name": self.server.name}])
		self.assertNotIn(peer.client_address, residents.get(self.server.name, []))


class TestInfraRoleExclusivity(_GatewayFixture):
	def test_vm_cannot_be_proxy_and_gateway(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			make_virtual_machine(
				self.server, self.image, title="both", status="Running", is_proxy=1, is_gateway=1
			)
