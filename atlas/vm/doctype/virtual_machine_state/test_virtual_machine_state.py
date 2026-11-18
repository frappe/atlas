from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase
from frappe.utils import now_datetime

from atlas.vm.doctype.virtual_machine_state import virtual_machine_state as virtual_machine_state_module
from atlas.vm.doctype.virtual_machine_state.virtual_machine_state import VirtualMachineState


class TestVirtualMachineState(UnitTestCase):
	def test_cpu_burst_fields_read_metal_only_for_a_burstable_vm(self) -> None:
		"""CPU burst fields are empty without a Metal call for a VM that is not burstable, and a save never reads them."""
		state = frappe.new_doc("Virtual Machine State")
		state.update(
			{
				"virtual_machine": "vm-cpu-burst-test",
				"server": "01a11636-0000-7000-8000-000000000000",
				"status": "running",
				"synced_at": now_datetime(),
			}
		)

		with (
			patch.object(virtual_machine_state_module, "VirtualMachineService") as service,
			patch.object(virtual_machine_state_module.frappe.db, "get_value", return_value=0),
		):
			self.assertIsNone(state.baseline_cpu_millicores)
			self.assertFalse(state.is_limited)
		service.assert_not_called()

		state.flags.ignore_links = True
		with patch.object(
			VirtualMachineState, "get_cpu_burst_status", side_effect=AssertionError("read Metal")
		):
			try:
				state.insert(ignore_permissions=True)
			finally:
				frappe.db.rollback()
