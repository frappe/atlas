from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.metal_server.core.public_ip_service import PublicIPService, generate_available_allocations


def insert_pool(prefix: str, allocation_prefix_length: int):
	return frappe.get_doc(
		{
			"doctype": "Public IP Pool",
			"prefix": prefix,
			"allocation_prefix_length": allocation_prefix_length,
			"source": "Static",
			"enabled": 1,
		}
	).insert(ignore_permissions=True)


class TestPublicIPAllocation(IntegrationTestCase):
	def test_allocator_advances_through_an_ipv4_pool(self) -> None:
		pool = insert_pool("198.19.40.0/30", 32)
		service = PublicIPService()

		first = service._create_next(pool)
		second = service._create_next(pool)

		self.assertEqual(first.prefix, "198.19.40.0/32")
		self.assertEqual(second.prefix, "198.19.40.1/32")
		self.assertEqual(pool.next_allocation_offset, "2")

	def test_reservation_claims_one_direct_allocation(self) -> None:
		insert_pool("198.19.41.0/30", 32)

		allocation = PublicIPService().reserve(7, 4)

		self.assertEqual(allocation.tenant_id, 7)
		self.assertEqual(allocation.status, "Reserved")
		self.assertTrue(allocation.is_reserved)

	def test_a_claim_takes_a_free_allocation_before_it_grows_the_pool(self) -> None:
		pool = insert_pool("198.19.45.0/30", 32)
		generate_available_allocations(pool.name, 2)

		allocation = PublicIPService().reserve(7, 4)

		self.assertIn(allocation.prefix, {"198.19.45.0/32", "198.19.45.1/32"})
		self.assertEqual(frappe.db.get_value("Public IP Pool", pool.name, "next_allocation_offset"), "2")

	def test_a_claim_whose_random_pick_misses_takes_a_free_allocation(self) -> None:
		pool = insert_pool("198.19.46.0/31", 32)
		generate_available_allocations(pool.name)

		with patch.object(PublicIPService, "_lock_random_available", return_value=None):
			allocation = PublicIPService().reserve(7, 4)

		self.assertEqual(allocation.status, "Reserved")
		self.assertEqual(frappe.db.count("Public IP Allocation", {"pool": pool.name}), 2)

	def test_allocator_stops_at_the_end_of_the_pool(self) -> None:
		pool = insert_pool("198.19.42.1/32", 32)
		service = PublicIPService()
		service._create_next(pool)

		with self.assertRaisesRegex(frappe.ValidationError, "empty"):
			service._create_next(pool)

	def test_direct_ipv6_reservation_keeps_the_configured_prefix_size(self) -> None:
		pool = insert_pool("2001:db8:42::/64", 80)

		with patch.object(PublicIPService, "_select_direct_pool", return_value=pool):
			allocation = PublicIPService().reserve(7, 6)

		self.assertEqual(allocation.prefix, "2001:db8:42::/80")
		self.assertEqual(allocation.status, "Reserved")

	def test_manual_generation_stops_at_the_end_of_the_pool(self) -> None:
		pool = insert_pool("198.19.43.0/30", 32)

		created = generate_available_allocations(pool.name)

		self.assertEqual(created, 4)
		self.assertEqual(frappe.db.count("Public IP Allocation", {"pool": pool.name}), 4)
		self.assertEqual(generate_available_allocations(pool.name), 0)

	def test_a_blocked_address_is_never_allocated(self) -> None:
		pool = insert_pool("198.19.44.0/31", 32)
		generate_available_allocations(pool.name)
		blocked = frappe.get_doc("Public IP Allocation", {"pool": pool.name, "prefix": "198.19.44.0/32"})
		blocked.block()

		reserved = PublicIPService().reserve(7, 4)
		with self.assertRaisesRegex(frappe.ValidationError, "capacity"):
			PublicIPService().reserve(7, 4)

		self.assertEqual(reserved.prefix, "198.19.44.1/32")
		self.assertEqual(frappe.db.get_value("Public IP Allocation", blocked.name, "status"), "Blocked")
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("Public IP Allocation", reserved.name).block()

		blocked.reload()
		blocked.unblock()
		self.assertEqual(PublicIPService().reserve(7, 4).name, blocked.name)
