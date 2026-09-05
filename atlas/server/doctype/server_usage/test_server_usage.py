from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.server.usage import (
	delete_old_usage_samples,
	enqueue_server_syncs,
	get_desired_images,
	get_privileged_vm_addresses,
	get_usage_values,
	sync_server,
)


class TestServerUsage(UnitTestCase):
	def test_enqueue_uses_one_exchange_per_server(self) -> None:
		peers = [{"node": "server-1"}]
		addresses = ["fdaa:1::1"]
		with (
			patch("atlas.server.usage.frappe.get_all", return_value=["server-1"]),
			patch("atlas.server.usage.get_wireguard_peers", return_value=peers),
			patch("atlas.server.usage.get_privileged_vm_addresses", return_value=addresses),
			patch("atlas.server.usage.frappe.enqueue") as enqueue,
		):
			enqueue_server_syncs()

		enqueue.assert_called_once_with(
			sync_server,
			queue="default",
			timeout=30,
			server_name="server-1",
			wireguard_peers=peers,
			privileged_vm_addresses=addresses,
			job_id="atlas||server-sync||server-1",
			deduplicate=True,
		)

	def test_privileged_addresses_select_live_privileged_vms(self) -> None:
		"""Every host holds the same whitelist, so one region-wide set goes out."""
		with (
			patch("atlas.server.usage.frappe.get_all", return_value=["vm-00001"]) as get_all,
			patch("atlas.server.usage.frappe.get_doc", return_value=Mock()),
			patch(
				"atlas.server.usage.VirtualMachineManager.get_wireguard_mesh_ipv6",
				return_value="fdaa:1:0:0::1",
			),
		):
			addresses = get_privileged_vm_addresses()

		self.assertEqual(addresses, ["fdaa:1:0:0::1"])
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{"is_privileged": 1, "is_draft": 0, "is_terminating": 0},
		)

	def test_desired_images_only_select_available_cached_images(self) -> None:
		image = Mock()
		image.get_desired_image.return_value = {"ref": "sha256:image", "cache_image": True}
		with (
			patch("atlas.server.usage.frappe.get_all", return_value=["image-1"]) as get_all,
			patch("atlas.server.usage.frappe.get_doc", return_value=image),
		):
			images = get_desired_images()

		self.assertEqual(images, [{"ref": "sha256:image", "cache_image": True}])
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{"enabled": 1, "status": "Available", "cache_image": 1},
		)

	def test_capacity_parses_the_metal_response(self) -> None:
		capacity = {
			"total_cpu_count": 8,
			"available_cpu_count": 6,
			"virtual_machine_count": 1,
			"total_memory_mib": 16384,
			"available_memory_mib": 12288,
			"total_storage_mib": 100000,
			"available_storage_mib": 80000,
		}

		self.assertEqual(get_usage_values(capacity)["available_cpu_count"], 6)

	def test_capacity_rejects_boolean_values(self) -> None:
		capacity = {
			"total_cpu_count": 8,
			"available_cpu_count": True,
			"virtual_machine_count": 1,
			"total_memory_mib": 16384,
			"available_memory_mib": 12288,
			"total_storage_mib": 100000,
			"available_storage_mib": 80000,
		}

		with self.assertRaises(ValueError):
			get_usage_values(capacity)

	def test_cleanup_deletes_samples_older_than_three_hours(self) -> None:
		current_time = datetime(2026, 9, 3, 12)

		with (
			patch("atlas.server.usage.now_datetime", return_value=current_time),
			patch("atlas.server.usage.frappe.db.delete") as delete,
		):
			delete_old_usage_samples()

		delete.assert_called_once_with(
			"Server Usage",
			{"creation": ["<", current_time - timedelta(hours=3)]},
		)
