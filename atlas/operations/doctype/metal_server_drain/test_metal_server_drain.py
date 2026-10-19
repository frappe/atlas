# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from atlas.operations.doctype.metal_server_drain.metal_server_drain import MetalServerDrain


def insert_server() -> str:
	name = str(uuid4())
	frappe.get_doc(
		{
			"doctype": "Metal Server",
			"name": name,
			"title": name,
			"server_size": "test-size",
			"server_image": "test-image",
		}
	).db_insert()
	return name


class TestMetalServerDrain(IntegrationTestCase):
	def setUp(self) -> None:
		self.addCleanup(frappe.db.rollback)
		self.source = insert_server()
		self.destination = insert_server()

	def new_drain(self, destinations: list[str]) -> MetalServerDrain:
		return frappe.get_doc(
			{
				"doctype": "Metal Server Drain",
				"source_metal_server": self.source,
				"allow_any_destination": 0,
				"destinations": [{"metal_server": server} for server in destinations],
			}
		)

	def test_rejects_the_source_as_a_destination(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be a destination"):
			self.new_drain([self.source]).insert()

	def test_rejects_a_duplicate_destination(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "only once"):
			self.new_drain([self.destination, self.destination]).insert()

	def test_requires_a_destination_unless_any_is_allowed(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "Add a destination"):
			self.new_drain([]).insert()

		drain = self.new_drain([])
		drain.allow_any_destination = 1
		drain.insert()
