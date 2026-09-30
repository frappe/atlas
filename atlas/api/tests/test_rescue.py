from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.api.models import VirtualMachineDetailResponse
from atlas.api.routes.virtual_machines import set_virtual_machine_rescue
from atlas.api.tests.test_support import TENANT_ID, api_request, call_route
from atlas.api.tests.test_virtual_machines import build_metal_information, build_virtual_machine


class TestRescueAPI(UnitTestCase):
	def test_route_uses_the_owned_vm_and_returns_acceptance(self):
		machine = build_virtual_machine(set_rescue=Mock())
		with (
			api_request(method="PUT", tenant_id=TENANT_ID, json={"enabled": True}),
			patch("atlas.api.routes.virtual_machines.get_owned_document", return_value=machine) as owned,
			patch("atlas.api.models.public_ip_allocations_for", return_value={}),
			patch("atlas.api.models.read_tags", return_value={}),
		):
			status, body = call_route(set_virtual_machine_rescue, "vm-00001")
		self.assertEqual(status, 202, body)
		owned.assert_called_once_with("Virtual Machine", "vm-00001")
		machine.set_rescue.assert_called_once_with(True)

	def test_route_rejects_missing_or_coerced_boolean(self):
		for body in ({}, {"enabled": "true"}, {"enabled": 1}, {"enabled": True, "image": "mine"}):
			with api_request(method="PUT", tenant_id=TENANT_ID, json=body):
				status, result = call_route(set_virtual_machine_rescue, "vm-00001")
			self.assertEqual(status, 400, result)

	def test_detail_reports_an_unapplied_rescue_mode(self):
		information = build_metal_information()
		information.desired.rescue.enabled = True
		with (
			patch("atlas.api.models.public_ip_allocations_for", return_value={}),
			patch("atlas.api.models.read_tags", return_value={}),
		):
			detail = VirtualMachineDetailResponse.from_document_and_metal(
				build_virtual_machine(), information
			)
		self.assertTrue(detail.rescue.enabled)
		self.assertFalse(detail.rescue.observed_enabled)
		self.assertTrue(detail.rescue.pending)
