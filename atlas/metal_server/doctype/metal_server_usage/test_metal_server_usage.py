from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.metal_server.usage import (
	delete_old_usage_samples,
	enqueue_server_sync,
	enqueue_server_syncs,
	get_desired_images,
	get_desired_unicast_peers,
	get_privileged_vm_addresses,
	get_usage_values,
	sync_server,
)
from atlas.vm.core.metal_client import MetalClientError

CAPACITY = {
	"total_cpu_millicores": 8000,
	"available_cpu_millicores": 6000,
	"virtual_machine_count": 1,
	"total_memory_mib": 16384,
	"available_memory_mib": 8192,
	"total_storage_mib": 102400,
	"available_storage_mib": 51200,
}


class TestServerUsage(UnitTestCase):
	def test_sync_logs_a_metal_connection_failure(self) -> None:
		server = SimpleNamespace(name="server-1")
		client = Mock()
		client.sync.side_effect = MetalClientError("connection refused")

		with (
			patch("atlas.metal_server.usage.frappe.get_doc", return_value=server),
			patch("atlas.metal_server.usage.MetalClient", return_value=client),
			patch("atlas.metal_server.usage.get_desired_images", return_value=[]),
			patch("atlas.metal_server.usage.frappe.log_error") as log_error,
		):
			sync_server("server-1", [], [])

		self.assertEqual(log_error.call_args.args[1], "Metal synchronization failed for Server server-1")

	def test_sync_logs_an_invalid_capacity_response(self) -> None:
		server = SimpleNamespace(name="server-1")
		client = Mock()
		client.sync.return_value = {"capacity": {}}

		with (
			patch("atlas.metal_server.usage.frappe.get_doc", return_value=server),
			patch("atlas.metal_server.usage.MetalClient", return_value=client),
			patch("atlas.metal_server.usage.get_desired_images", return_value=[]),
			patch("atlas.metal_server.usage.frappe.log_error") as log_error,
		):
			sync_server("server-1", [], [])

		self.assertEqual(log_error.call_args.args[1], "Invalid synchronization response from Server server-1")

	def test_sync_logs_an_invalid_virtual_machine_response(self) -> None:
		server = SimpleNamespace(name="server-1")
		client = Mock()
		client.sync.return_value = {"capacity": CAPACITY, "virtual_machines": {"vm-00001": {}}}

		with (
			patch("atlas.metal_server.usage.frappe.get_doc", return_value=server),
			patch("atlas.metal_server.usage.MetalClient", return_value=client),
			patch("atlas.metal_server.usage.get_desired_images", return_value=[]),
			patch("atlas.metal_server.usage.store_reported_states", side_effect=ValueError("bad")),
			patch("atlas.metal_server.usage.frappe.log_error") as log_error,
		):
			sync_server("server-1", [], [])

		self.assertEqual(log_error.call_args.args[1], "Invalid synchronization response from Server server-1")

	def test_enqueue_uses_one_exchange_per_server(self) -> None:
		peers = [{"node": "server-1"}]
		addresses = ["fdaa:1::1"]
		with (
			patch("atlas.metal_server.usage.frappe.get_all", return_value=["server-1"]),
			patch("atlas.metal_server.usage.get_wireguard_peers", return_value=peers),
			patch("atlas.metal_server.usage.get_privileged_vm_addresses", return_value=addresses),
			patch("atlas.metal_server.usage.get_desired_unicast_peers", return_value=None),
			patch("atlas.metal_server.usage.frappe.enqueue") as enqueue,
		):
			enqueue_server_syncs()

		enqueue.assert_called_once_with(
			sync_server,
			queue="default",
			timeout=30,
			server_name="server-1",
			wireguard_peers=peers,
			privileged_vm_addresses=addresses,
			unicast_peers=None,
			job_id="atlas||server-sync||server-1",
			deduplicate=True,
		)

	# The queued sync reads the shared sets itself.
	def test_one_server_exchange_reads_the_shared_sets(self) -> None:
		peers = [{"node": "server-1"}]
		addresses = ["fdaa:1::1"]
		unicast_peers = ["10.20.0.11"]
		with (
			patch("atlas.metal_server.usage.get_wireguard_peers", return_value=peers),
			patch("atlas.metal_server.usage.get_privileged_vm_addresses", return_value=addresses),
			patch("atlas.metal_server.usage.get_desired_unicast_peers", return_value=unicast_peers),
			patch("atlas.metal_server.usage.frappe.enqueue") as enqueue,
		):
			enqueue_server_sync("server-1")

		self.assertEqual(enqueue.call_args.kwargs["wireguard_peers"], peers)
		self.assertEqual(enqueue.call_args.kwargs["privileged_vm_addresses"], addresses)
		self.assertEqual(enqueue.call_args.kwargs["unicast_peers"], unicast_peers)
		self.assertEqual(enqueue.call_args.kwargs["job_id"], "atlas||server-sync||server-1")

	def test_unicast_peers_reach_the_host_exchange(self) -> None:
		"""Only unicast mode sends the field, because Metal decodes the request strictly."""
		server = SimpleNamespace(name="server-1")
		client = Mock()
		client.sync.return_value = {"capacity": CAPACITY, "virtual_machines": {}}
		usage_document = Mock()

		def get_document(*arguments):
			if isinstance(arguments[0], str):
				return server
			return usage_document

		with (
			patch("atlas.metal_server.usage.frappe.get_doc", side_effect=get_document),
			patch("atlas.metal_server.usage.MetalClient", return_value=client),
			patch("atlas.metal_server.usage.get_desired_images", return_value=[]),
			patch("atlas.metal_server.usage.store_reported_states"),
		):
			sync_server("server-1", [], [], unicast_peers=["10.20.0.11"])

		self.assertEqual(client.sync.call_args.args[3], ["10.20.0.11"])
		usage_document.insert.assert_called_once_with(ignore_permissions=True)

	def test_desired_unicast_peers_follow_the_settings_flag(self) -> None:
		settings = SimpleNamespace(is_unicast_network_enabled=False)

		with (
			patch("atlas.metal_server.usage.frappe.get_single", return_value=settings),
			patch("atlas.metal_server.usage.get_mesh_peers") as get_mesh_peers,
		):
			self.assertIsNone(get_desired_unicast_peers())

		get_mesh_peers.assert_not_called()

		settings.is_unicast_network_enabled = True
		with (
			patch("atlas.metal_server.usage.frappe.get_single", return_value=settings),
			patch(
				"atlas.metal_server.usage.get_mesh_peers",
				return_value=["10.20.0.11"],
			) as get_mesh_peers,
		):
			self.assertEqual(get_desired_unicast_peers(), ["10.20.0.11"])

		get_mesh_peers.assert_called_once_with()

	def test_privileged_addresses_select_live_privileged_vms(self) -> None:
		"""Every host holds the same whitelist, so one region-wide set goes out."""
		rows = [{"name": "vm-00001", "tenant_id": 0}]
		with (
			patch("atlas.metal_server.usage.frappe.get_all", return_value=rows) as get_all,
			patch(
				"atlas.metal_server.usage.get_virtual_machine_mesh_address",
				return_value="fdaa:1:0:0::1",
			),
			patch("atlas.metal_server.usage.frappe.get_doc") as get_doc,
		):
			addresses = get_privileged_vm_addresses()

		self.assertEqual(addresses, ["fdaa:1:0:0::1"])
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{"is_privileged": 1, "is_draft": 0, "is_terminating": 0},
		)
		# One query keeps address lookup independent of VM count.
		self.assertEqual(get_all.call_args.kwargs["fields"], ["name", "tenant_id"])
		get_doc.assert_not_called()

	def test_desired_images_only_select_available_cached_images(self) -> None:
		image = Mock()
		image.get_desired_image.return_value = {"ref": "sha256:image", "cache_image": True}
		with (
			patch("atlas.metal_server.usage.frappe.get_all", return_value=["image-1"]) as get_all,
			patch("atlas.metal_server.usage.frappe.get_doc", return_value=image),
		):
			images = get_desired_images()

		self.assertEqual(images, [{"ref": "sha256:image", "cache_image": True}])
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{"enabled": 1, "status": "Available", "cache_image": 1},
		)

	def test_capacity_parses_the_metal_response(self) -> None:
		capacity = {
			"total_cpu_millicores": 8000,
			"available_cpu_millicores": 6000,
			"virtual_machine_count": 1,
			"total_memory_mib": 16384,
			"available_memory_mib": 12288,
			"total_storage_mib": 100000,
			"available_storage_mib": 80000,
		}

		self.assertEqual(get_usage_values(capacity)["available_cpu_millicores"], 6000)

	def test_capacity_rejects_boolean_values(self) -> None:
		capacity = {
			"total_cpu_millicores": 8000,
			"available_cpu_millicores": True,
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
			patch("atlas.metal_server.usage.now_datetime", return_value=current_time),
			patch("atlas.metal_server.usage.frappe.db.delete") as delete,
		):
			delete_old_usage_samples()

		delete.assert_called_once_with(
			"Metal Server Usage",
			{"creation": ["<", current_time - timedelta(hours=3)]},
		)
