from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.atlas.core.exceptions import AtlasConflictError, AtlasUserError
from atlas.vm.core.metal_models import MetalRescue, MetalVirtualMachine
from atlas.vm.core.test_metal_models import complete_response
from atlas.vm.core.vm_service import VirtualMachineService
from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage


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

	def test_new_session_uses_tag_lookup_and_requires_compatible_image(self):
		image = Mock(architecture="amd64")
		with (
			patch(
				"atlas.vm.core.vm_service.frappe.get_all",
				return_value=["rescue-1"],
			),
			patch.object(self.service, "get_image", return_value=image) as get_image,
		):
			self.service.set_rescue(True)
			get_image.assert_called_once_with("rescue-1", 7)
			image.validate_rescue_image.assert_called_once()
			self.client.set_virtual_machine_rescue.assert_called_once_with(
				"vm-1",
				True,
				image.get_metal_image_request.return_value,
			)
			image.architecture = "arm64"
			with self.assertRaises(AtlasUserError):
				self.service.set_rescue(True)

	def test_repeat_does_not_change_pinned_image_after_tagged_image_changes(self):
		image = object.__new__(VirtualMachineImage)
		for key, value in {
			"name": "rescue-1",
			"title": "Rescue image",
			"architecture": "amd64",
			"image_type": "system",
			"status": "Available",
			"enabled": 1,
			"cache_image": 0,
			"memory_snapshot": 0,
			"artifact_storage": "Object Storage",
			"image_object_key": "images/rescue-1/rootfs",
			"kernel_object_key": "images/rescue-1/kernel",
			"image_sha256": "a" * 64,
			"kernel_sha256": "b" * 64,
			"tags": [SimpleNamespace(key="purpose", value="rescue")],
		}.items():
			setattr(image, key, value)
		pinned = replace(self.current.desired.image, ref=image.immutable_reference, memory_snapshot=False)
		self.information.return_value = replace(
			self.current,
			desired=replace(self.current.desired, state="stopped", rescue=MetalRescue(True, pinned)),
		)
		with (
			patch(
				"atlas.vm.core.vm_service.frappe.get_all",
				return_value=["new-default"],
			) as image_query,
			patch("atlas.vm.core.vm_service.frappe.db.get_value", return_value=image.name) as lookup,
			patch("atlas.vm.core.vm_service.frappe.get_doc", return_value=image),
			patch.object(image, "get_object_url") as sign,
		):
			for renewal in (1, 2):
				sign.side_effect = lambda key, expiry: f"https://images.example/{key}?renewal={renewal}"
				self.service.set_rescue(True)
				request = self.client.set_virtual_machine_rescue.call_args.args[2]
				self.assertEqual(request["ref"], pinned.ref)
				self.assertEqual(request["rootfs"]["sha256"], pinned.rootfs.sha256)
				self.assertEqual(request["kernel"]["sha256"], pinned.kernel.sha256)
				for artifact in ("rootfs", "kernel"):
					self.assertTrue(request[artifact]["url"].endswith(f"renewal={renewal}"))
			self.assertNotIn("Virtual Machine Image", [call.args[0] for call in image_query.call_args_list])
			lookup.assert_called_with(
				"Virtual Machine Image",
				{
					"image_type": "system",
					"status": "Available",
					"enabled": 1,
					"architecture": pinned.architecture,
					"image_sha256": pinned.rootfs.sha256,
					"kernel_sha256": pinned.kernel.sha256,
				},
				"name",
			)
		self.client.set_virtual_machine_power_state.assert_not_called()

	def test_missing_pinned_image_does_not_fall_back_to_the_default(self):
		for pinned in (self.current.desired.image, None):
			with self.subTest(pinned=pinned):
				self.information.return_value = replace(
					self.current,
					desired=replace(self.current.desired, rescue=MetalRescue(True, pinned)),
				)
				with (
					patch("atlas.vm.core.vm_service.frappe.get_all") as image_query,
					patch("atlas.vm.core.vm_service.frappe.db.get_value", return_value=None),
					self.assertRaises(AtlasConflictError),
				):
					self.service.set_rescue(True)
				self.assertNotIn(
					"Virtual Machine Image", [call.args[0] for call in image_query.call_args_list]
				)
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
		with patch("atlas.vm.core.vm_service.frappe.get_all") as image_query:
			self.service.set_rescue(False)
		self.assertNotIn("Virtual Machine Image", [call.args[0] for call in image_query.call_args_list])
		self.client.set_virtual_machine_rescue.assert_called_once_with("vm-1", False, None)

	def test_resize_migration_and_snapshot_guard_waits_for_exit(self):
		for desired in (
			replace(self.current.desired, rescue=MetalRescue(True, None)),
			replace(self.current.desired, rescue_generation=2),
		):
			with self.assertRaises(AtlasConflictError):
				self.service.ensure_rescue_inactive(replace(self.current, desired=desired))
