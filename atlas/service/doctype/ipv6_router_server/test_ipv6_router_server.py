from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import frappe
from frappe.tests import UnitTestCase

import atlas.service.doctype.ipv6_router_server.ipv6_router_server as router_module
from atlas.service.doctype.ipv6_router_server.ipv6_router_server import IPv6RouterServer


def public_ip_pool(**values) -> SimpleNamespace:
	return SimpleNamespace(
		**(
			{
				"name": "pool-1",
				"version": "6",
				"gateway": None,
				"prefix": "2001:db8::/64",
				"allocation_prefix_length": 64,
				"save": Mock(),
			}
			| values
		)
	)


class TestIPv6PoolValidation(UnitTestCase):
	def validate(self, pool: SimpleNamespace, has_allocations: bool = False) -> None:
		def exists(doctype, *_args, **_kwargs):
			return doctype == "Public IP Pool" or has_allocations

		with (
			patch.object(router_module.frappe.db, "exists", side_effect=exists),
			patch.object(router_module.frappe, "get_doc", return_value=pool),
		):
			router_module._validate_ipv6_pool("pool-1")

	def test_a_free_ipv6_pool_is_accepted(self) -> None:
		self.validate(public_ip_pool())

	def test_a_pool_with_a_gateway_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "no gateway or allocations"):
			self.validate(public_ip_pool(gateway="ipv6-router-002"))

	def test_a_pool_with_allocations_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "no gateway or allocations"):
			self.validate(public_ip_pool(), has_allocations=True)

	def test_a_small_pool_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "/84"):
			self.validate(public_ip_pool(prefix="2001:db8::/96"))


class TestIPv6RouterCreation(UnitTestCase):
	def test_router_stores_the_mesh_address_of_its_virtual_machine(self) -> None:
		router_server = SimpleNamespace()
		with patch.object(router_module, "get_virtual_machine_mesh_address", return_value="fdaa:1::1"):
			IPv6RouterServer._set_virtual_machine(router_server, "vm-00001")
		self.assertEqual(router_server.virtual_machine, "vm-00001")
		self.assertEqual(router_server.wireguard_mesh_ipv6, "fdaa:1::1")

	def test_the_pool_link_is_committed_before_vm_placement(self) -> None:
		router_server = MagicMock(name="router_server")
		router_server.name = "ipv6-router-001"
		router_server.flags = SimpleNamespace()
		router_server.status = "Pending"
		pool = public_ip_pool()
		calls: list[str] = []
		router_server.insert.side_effect = lambda *_, **__: calls.append("insert")
		pool.save.side_effect = lambda *_, **__: calls.append("save-pool")
		router_server._create_virtual_machine.side_effect = lambda *_: (calls.append("create-vm"), False)[1]

		with (
			patch.object(router_module, "_validate_system_manager"),
			patch.object(router_module, "_validate_create_request"),
			patch.object(router_module.frappe, "new_doc", return_value=router_server),
			patch.object(router_module.frappe, "get_doc", return_value=pool),
			patch.object(router_module.frappe.db, "commit", side_effect=lambda: calls.append("commit")),
		):
			result = IPv6RouterServer.create({"public_ip_pool": "pool-1"})

		self.assertEqual(result, {"name": "ipv6-router-001", "is_draft": False})
		self.assertEqual(calls[:4], ["insert", "save-pool", "commit", "create-vm"])
		self.assertEqual(pool.gateway, "ipv6-router-001")
		self.assertEqual(pool.allocation_prefix_length, 128)

	def test_pending_and_interrupted_provisioning_are_queued(self) -> None:
		router_server = MagicMock()
		with (
			patch.object(router_module.frappe, "get_all", return_value=["ipv6-router-001"]),
			patch.object(router_module.frappe, "get_doc", return_value=router_server),
		):
			router_module.enqueue_pending_ipv6_router_provisioning()
		router_server.enqueue_provisioning.assert_called_once_with(enqueue_after_commit=False)


class TestIPv6RouterArchive(UnitTestCase):
	def test_archive_queues_the_pool_detach_instead_of_running_it(self) -> None:
		pool = MagicMock()
		router = MagicMock(status="Failed", virtual_machine=None, pool=pool)
		with (
			patch.object(router_module, "_validate_system_manager"),
			patch.object(router_module, "ipv6_router_lifecycle_lock", return_value=nullcontext()),
			patch.object(router_module.frappe, "get_doc", return_value=router),
			patch.object(router_module.frappe.db, "exists", return_value=False),
			patch.object(router_module.frappe, "msgprint"),
		):
			IPv6RouterServer.archive(SimpleNamespace(doctype="IPv6 Router Server", name="ipv6-router-001"))

		pool.begin_provider_detach.assert_called_once_with()
		pool.queue_reconcile.assert_called_once_with()
		pool.reconcile.assert_not_called()
		self.assertEqual(router.status, "Archived")

	def test_archive_returns_a_provider_pool_to_one_allocation(self) -> None:
		pool = MagicMock(source="Provider", prefix="2001:db8::/80", allocation_prefix_length=128)
		router = MagicMock(status="Failed", virtual_machine=None, pool=pool)
		with (
			patch.object(router_module, "_validate_system_manager"),
			patch.object(router_module, "ipv6_router_lifecycle_lock", return_value=nullcontext()),
			patch.object(router_module.frappe, "get_doc", return_value=router),
			patch.object(router_module.frappe.db, "exists", return_value=False),
			patch.object(router_module.frappe, "msgprint"),
		):
			IPv6RouterServer.archive(SimpleNamespace(doctype="IPv6 Router Server", name="ipv6-router-001"))

		self.assertIsNone(pool.gateway)
		self.assertEqual(pool.allocation_prefix_length, 80)
		pool.save.assert_called_once_with(ignore_permissions=True)
