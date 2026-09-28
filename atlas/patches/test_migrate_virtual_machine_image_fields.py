from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from atlas.api.routes.images import list_images
from atlas.api.tests.test_support import TENANT_ID, api_request, call_route
from atlas.patches.migrate_virtual_machine_image_fields import execute


def compact(query: str) -> str:
	"""Return one line of SQL for direct assertions."""
	return " ".join(query.split())


class TestMigrateVirtualMachineImageFields(UnitTestCase):
	@patch("atlas.patches.migrate_virtual_machine_image_fields.frappe.db.has_column", return_value=True)
	@patch("atlas.patches.migrate_virtual_machine_image_fields.frappe.db.sql")
	def test_legacy_values_move_to_the_current_fields(self, sql, _has_column) -> None:
		execute()

		self.assertEqual(sql.call_count, 2)
		self.assertEqual(
			compact(sql.call_args_list[0].args[0]),
			"UPDATE `tabVirtual Machine Image` SET `image_type` = LOWER(`image_type`) "
			"WHERE BINARY `image_type` IN ('System', 'Machine')",
		)
		self.assertEqual(
			compact(sql.call_args_list[1].args[0]),
			"UPDATE `tabVirtual Machine Image` SET `architecture` = `platform` "
			"WHERE COALESCE(`architecture`, '') = '' AND `platform` IN ('amd64', 'arm64')",
		)

	@patch("atlas.patches.migrate_virtual_machine_image_fields.frappe.db.has_column", return_value=False)
	@patch("atlas.patches.migrate_virtual_machine_image_fields.frappe.db.sql")
	def test_a_fresh_schema_skips_the_removed_platform_field(self, sql, _has_column) -> None:
		execute()

		sql.assert_called_once()


class IntegrationTestMigrateVirtualMachineImageFields(IntegrationTestCase):
	def test_only_legacy_values_change(self) -> None:
		if not frappe.db.has_column("Virtual Machine Image", "platform"):
			self.skipTest("The fresh schema has no legacy platform field.")

		legacy = self.insert_image("system", "amd64")
		modern = self.insert_image("machine", "arm64")
		frappe.db.sql(
			"""
			UPDATE `tabVirtual Machine Image`
			SET `image_type` = 'System', `architecture` = NULL, `platform` = 'amd64'
			WHERE `name` = %s
			""",
			legacy,
		)
		frappe.db.sql(
			"UPDATE `tabVirtual Machine Image` SET `platform` = 'amd64' WHERE `name` = %s",
			modern,
		)

		execute()

		self.assertEqual(self.image_fields(legacy), {"image_type": "system", "architecture": "amd64"})
		self.assertEqual(self.image_fields(modern), {"image_type": "machine", "architecture": "arm64"})
		with api_request("GET", "/api/atlas/images", tenant_id=TENANT_ID, query_string={"limit": "100"}):
			status, body = call_route(list_images)

		self.assertEqual(status, 200)
		self.assertIn(legacy, {image["id"] for image in body["items"]})

	@staticmethod
	def insert_image(image_type: str, architecture: str) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Virtual Machine Image",
					"title": f"migration-test-{image_type}-{architecture}",
					"tenant_id": 0,
					"image_type": image_type,
					"architecture": architecture,
					"status": "Pending",
					"enabled": 1,
				}
			)
			.insert()
			.name
		)

	@staticmethod
	def image_fields(name: str) -> dict[str, str]:
		return frappe.db.get_value(
			"Virtual Machine Image", name, ["image_type", "architecture"], as_dict=True
		)
