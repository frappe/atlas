from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.atlas.core.exceptions import AtlasConflictError, AtlasUserError
from atlas.vm.core.metal_models import MetalRescue, MetalVirtualMachine
from atlas.vm.core.test_metal_models import complete_response
from atlas.vm.core.vm_service import VirtualMachineService


class TestRescueSelection(UnitTestCase):
	def setUp(self):
		self.service = VirtualMachineService(SimpleNamespace(name="vm-1", tenant_id=7, architecture="amd64"))
		self.current = MetalVirtualMachine.from_dict(complete_response())
		self.client = Mock()
		self.client.set_virtual_machine_rescue.return_value = self.current
		self.enterContext(patch.object(VirtualMachineService, "metal_client", self.client))
		self.information = self.enterContext(
			patch.object(self.service, "require_information", return_value=self.current)
		)

	def test_new_session_uses_settings_and_requires_compatible_image(self):
		image = Mock(architecture="amd64")
		with (
			patch(
				"atlas.vm.core.vm_service.frappe.get_single",
				return_value=SimpleNamespace(rescue_virtual_machine_image="rescue-1"),
			),
			patch.object(self.service, "get_image", return_value=image) as get_image,
		):
			self.service.set_rescue(True)
			get_image.assert_called_once_with("rescue-1", 7)
			image.validate_rescue_image.assert_called_once()
			self.client.set_virtual_machine_rescue.assert_called_once_with(
				"vm-1", True, image.get_metal_image_request.return_value
			)
			image.architecture = "arm64"
			with self.assertRaises(AtlasUserError):
				self.service.set_rescue(True)

	def test_repeat_does_not_change_pinned_image_after_setting_changes(self):
		self.information.return_value = replace(
			self.current,
			desired=replace(self.current.desired, rescue=MetalRescue(True, self.current.desired.image)),
		)
		with patch("atlas.vm.core.vm_service.frappe.get_single") as settings:
			self.service.set_rescue(True)
		settings.assert_not_called()
		self.client.set_virtual_machine_rescue.assert_not_called()

	def test_paused_vm_must_stop_first(self):
		self.information.return_value = replace(
			self.current, desired=replace(self.current.desired, state="paused")
		)
		with self.assertRaises(AtlasConflictError):
			self.service.set_rescue(True)
		self.client.set_virtual_machine_rescue.assert_not_called()

	def test_exit_does_not_require_the_image_record(self):
		self.information.return_value = replace(
			self.current,
			desired=replace(self.current.desired, rescue=MetalRescue(True, self.current.desired.image)),
		)
		with patch("atlas.vm.core.vm_service.frappe.get_single") as settings:
			self.service.set_rescue(False)
		settings.assert_not_called()
		self.client.set_virtual_machine_rescue.assert_called_once_with("vm-1", False, None)

	def test_resize_migration_and_snapshot_guard_waits_for_exit(self):
		for desired in (
			replace(self.current.desired, rescue=MetalRescue(True, None)),
			replace(self.current.desired, rescue_generation=2),
		):
			with self.assertRaises(AtlasConflictError):
				self.service.ensure_rescue_inactive(replace(self.current, desired=desired))
