from unittest.mock import Mock, patch

import frappe
from botocore.stub import ANY, Stubber
from frappe.tests import UnitTestCase

from atlas.atlas.object_storage import ObjectStorageClient, ObjectStorageError
from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.metal_models import MetalVirtualMachine
from atlas.vm.core.multipart_upload import MultipartUploadError
from atlas.vm.core.reconciliation import delete_virtual_machine
from atlas.vm.core.sleep_snapshot_objects import SleepSnapshotObjects
from atlas.vm.core.test_metal_models import complete_response
from atlas.vm.core.vm_sleep import VirtualMachineSleepService


class TestSleepSnapshotObjects(UnitTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.machine = frappe.get_doc({"doctype": "Virtual Machine", "tenant_id": 65534, "is_draft": 1})
		self.machine.flags.created_by_virtual_machine_api = True
		self.machine.insert(ignore_mandatory=True, ignore_permissions=True)
		self.addCleanup(self.remove_machine)
		self.objects = SleepSnapshotObjects(self.machine)
		self.storage = ObjectStorageClient(
			bucket="snapshots",
			access_key_id="test",
			secret_access_key="test",
			endpoint_url="https://objects.example",
			region="test",
		)

	def remove_machine(self) -> None:
		frappe.db.delete("Virtual Machine", {"name": self.machine.name})
		frappe.db.commit()  # nosemgrep

	def remember(self, checkpoint: str) -> None:
		response = complete_response()
		response["observed"]["sleep_snapshot"] = {
			"id": checkpoint,
			"memory": {
				"uploaded": False,
				"object_key": f"sleep/{checkpoint}/memory",
				"upload_id": checkpoint,
			},
		}
		self.objects.remember(MetalVirtualMachine.from_dict(response))

	def test_replacement_keeps_current_checkpoint_and_retries_failed_cleanup(self) -> None:
		self.remember("old")
		self.remember("current")
		stale_document = frappe.get_doc("Virtual Machine", self.machine.name)
		with Stubber(self.storage._client) as remote:
			remote.add_response(
				"abort_multipart_upload",
				{},
				{"Bucket": "snapshots", "Key": "sleep/old/memory", "UploadId": "old"},
			)
			remote.add_client_error(
				"delete_object",
				service_error_code="InternalError",
				http_status_code=500,
				expected_params={"Bucket": "snapshots", "Key": "sleep/old/memory"},
			)
			with patch.object(
				type(frappe.get_cached_doc("Atlas Settings")),
				"get_object_storage_client",
				return_value=self.storage,
			):
				with self.assertRaises(ObjectStorageError):
					self.objects.delete(keep_checkpoint="current")
			self.assertEqual(len(SleepSnapshotObjects(stale_document).load()), 2)
			remote.add_client_error(
				"abort_multipart_upload",
				service_error_code="NoSuchUpload",
				http_status_code=404,
				expected_params={"Bucket": "snapshots", "Key": "sleep/old/memory", "UploadId": "old"},
			)
			remote.add_response("delete_object", {}, {"Bucket": "snapshots", "Key": "sleep/old/memory"})
			with patch.object(
				type(frappe.get_cached_doc("Atlas Settings")),
				"get_object_storage_client",
				return_value=self.storage,
			):
				self.objects.delete(keep_checkpoint="current")
			remote.assert_no_pending_responses()
		self.assertEqual(
			[item["checkpoint_id"] for item in SleepSnapshotObjects(stale_document).load()], ["current"]
		)

	def test_termination_cleans_retained_objects_after_host_record_is_gone(self) -> None:
		self.remember("source-host")
		self.remember("destination-host")
		with Stubber(self.storage._client) as remote:
			for checkpoint in ("source-host", "destination-host"):
				remote.add_response(
					"abort_multipart_upload",
					{},
					{"Bucket": "snapshots", "Key": f"sleep/{checkpoint}/memory", "UploadId": checkpoint},
				)
				remote.add_response(
					"delete_object", {}, {"Bucket": "snapshots", "Key": f"sleep/{checkpoint}/memory"}
				)
			with patch.object(
				type(frappe.get_cached_doc("Atlas Settings")),
				"get_object_storage_client",
				return_value=self.storage,
			):
				delete_virtual_machine(self.machine, "test cleanup")
				self.objects.delete()
			remote.assert_no_pending_responses()
		self.assertEqual(self.objects.load(), [])
		self.assertFalse(frappe.db.exists("Virtual Machine", self.machine.name))

	def test_an_uncertain_metal_response_reuses_the_retained_upload(self) -> None:
		with Stubber(self.storage._client) as remote:
			remote.add_response(
				"create_multipart_upload", {"UploadId": "upload-1"}, {"Bucket": "snapshots", "Key": ANY}
			)
			with patch.object(
				type(frappe.get_cached_doc("Atlas Settings")),
				"get_object_storage_client",
				return_value=self.storage,
			):
				first = self.objects.prepare_upload("memory", "")
				second = SleepSnapshotObjects(self.machine).prepare_upload("memory", "")
			remote.assert_no_pending_responses()
		self.assertEqual(first, second)
		self.assertEqual(first["upload_id"], "upload-1")
		self.assertEqual(self.objects.load(), [first])

	def test_a_stale_vm_save_cannot_erase_or_replace_cleanup_references(self) -> None:
		stale = frappe.get_doc("Virtual Machine", self.machine.name)
		self.remember("current")
		expected = self.objects.load()
		stale.is_termination_protected = 1
		stale.sleep_snapshot_objects = [{"object_key": "another-vm/private-disk"}]
		stale.flags.ignore_mandatory = True
		stale.flags.ignore_validate = True
		stale.save(ignore_permissions=True)
		self.assertEqual(self.objects.load(), expected)
		self.assertEqual(
			frappe.db.get_value("Virtual Machine", self.machine.name, "is_termination_protected"), 1
		)

	def pending_upload(self) -> MetalVirtualMachine:
		response = complete_response()
		response["observed"]["sleep_snapshot"] = {
			"id": "pending",
			"memory": {
				"uploaded": False,
				"object_key": "sleep/pending/memory",
				"upload_id": "pending",
				"pending": {"stored_size_bytes": 128, "parts": [{"part_number": 1, "etag": "part-1"}]},
			},
		}
		return MetalVirtualMachine.from_dict(response)

	def test_completion_retries_a_lost_ack_without_waiting_for_url_refresh(self) -> None:
		information = self.pending_upload()
		client = Mock(spec=MetalClient)
		client.refresh_sleep_state.side_effect = [MetalClientError("lost acknowledgment"), information]
		cache_key = f"atlas:vm-sleep-urls:{self.machine.name}:pending"
		frappe.cache.set_value(cache_key, True, expires_in_sec=3600)
		self.addCleanup(frappe.cache.delete_value, cache_key)
		with Stubber(self.storage._client) as remote:
			remote.add_client_error("head_object", service_error_code="404", http_status_code=404)
			remote.add_response(
				"complete_multipart_upload",
				{},
				{
					"Bucket": "snapshots",
					"Key": "sleep/pending/memory",
					"UploadId": "pending",
					"MultipartUpload": {"Parts": [{"PartNumber": 1, "ETag": "part-1"}]},
				},
			)
			remote.add_response("head_object", {"ContentLength": 128})
			remote.add_response("head_object", {"ContentLength": 128})
			with patch.object(
				type(frappe.get_cached_doc("Atlas Settings")),
				"get_object_storage_client",
				return_value=self.storage,
			):
				service = VirtualMachineSleepService(self.machine)
				with self.assertRaises(MetalClientError):
					service.refresh_urls(information, client)
				service.refresh_urls(information, client)
			remote.assert_no_pending_responses()
		self.assertEqual(client.refresh_sleep_state.call_count, 2)
		request = client.refresh_sleep_state.call_args.args[1]
		self.assertEqual(request["memory"]["completed_upload_id"], "pending")
		client.offload_sleep_state.assert_not_called()

	def test_an_incorrect_final_object_size_is_not_acknowledged(self) -> None:
		client = Mock(spec=MetalClient)
		with Stubber(self.storage._client) as remote:
			remote.add_client_error("head_object", service_error_code="404", http_status_code=404)
			remote.add_response("complete_multipart_upload", {})
			remote.add_response("head_object", {"ContentLength": 129})
			with patch.object(
				type(frappe.get_cached_doc("Atlas Settings")),
				"get_object_storage_client",
				return_value=self.storage,
			):
				with self.assertRaises(MultipartUploadError):
					VirtualMachineSleepService(self.machine).refresh_urls(self.pending_upload(), client)
			remote.assert_no_pending_responses()
		client.refresh_sleep_state.assert_not_called()
		self.assertEqual(self.objects.load()[0]["upload_id"], "pending")
