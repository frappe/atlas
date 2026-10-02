from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.metal_server.usage import (
	delete_old_usage_samples,
	enqueue_server_sync,
	enqueue_server_syncs,
	get_desired_images,
	get_privileged_vm_addresses,
	get_usage_values,
	get_wireguard_peers,
	is_unicast_network_enabled,
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
			sync_server("server-1", [], [], False)

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
			sync_server("server-1", [], [], False)

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
			sync_server("server-1", [], [], False)

		self.assertEqual(log_error.call_args.args[1], "Invalid synchronization response from Server server-1")

	def test_enqueue_uses_one_exchange_per_server(self) -> None:
		peers = [{"node": "server-1"}]
		addresses = ["fdaa:1::1"]
		with (
			patch("atlas.metal_server.usage.frappe.get_all", return_value=["server-1"]),
			patch("atlas.metal_server.usage.get_wireguard_peers", return_value=peers),
			patch("atlas.metal_server.usage.get_privileged_vm_addresses", return_value=addresses),
			patch("atlas.metal_server.usage.is_unicast_network_enabled", return_value=False),
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
			unicast=False,
			job_id="atlas||server-sync||server-1",
			deduplicate=True,
		)

	# The queued sync reads the shared sets itself.
	def test_one_server_exchange_reads_the_shared_sets(self) -> None:
		peers = [{"node": "server-1"}]
		addresses = ["fdaa:1::1"]
		with (
			patch("atlas.metal_server.usage.get_wireguard_peers", return_value=peers),
			patch("atlas.metal_server.usage.get_privileged_vm_addresses", return_value=addresses),
			patch("atlas.metal_server.usage.is_unicast_network_enabled", return_value=True),
			patch("atlas.metal_server.usage.frappe.enqueue") as enqueue,
		):
			enqueue_server_sync("server-1")

		self.assertEqual(enqueue.call_args.kwargs["wireguard_peers"], peers)
		self.assertEqual(enqueue.call_args.kwargs["privileged_vm_addresses"], addresses)
		self.assertTrue(enqueue.call_args.kwargs["unicast"])
		self.assertEqual(enqueue.call_args.kwargs["job_id"], "atlas||server-sync||server-1")

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

	def test_wireguard_peers_carry_the_stored_mesh_address(self) -> None:
		"""Metal takes the address from Atlas, so it never derives one from the name."""
		rows = [
			frappe._dict(
				name="01a0c05f-e209-70ad-a183-dda2a727cd8b",
				wireguard_public_key="key-1",
				wireguard_ip_address="fdab:1:e209:70ad:a183:dda2:a727:cd8b",
				public_ipv4_address="203.0.113.7",
				private_ipv4_address="10.0.0.7",
				port=51820,
				private_network_mac_address="aa:bb:cc:dd:ee:07",
			)
		]

		with patch("atlas.metal_server.usage.frappe.get_all", return_value=rows):
			peers = get_wireguard_peers()

		self.assertEqual(
			peers,
			[
				{
					"node": "01a0c05f-e209-70ad-a183-dda2a727cd8b",
					"mesh_address": "fdab:1:e209:70ad:a183:dda2:a727:cd8b",
					"public_key": "key-1",
					"address": "10.0.0.7:51820",
					"public_address": "203.0.113.7",
					"private_address": "10.0.0.7",
					"private_network_mac_address": "aa:bb:cc:dd:ee:07",
				}
			],
		)

	def test_wireguard_peers_skip_a_server_without_a_mesh_address(self) -> None:
		"""A host that has not configured WireGuard yet cannot be an AllowedIPs entry."""
		rows = [
			frappe._dict(
				name="01a0c05f-e209-70ad-a183-dda2a727cd8b",
				wireguard_public_key="key-1",
				wireguard_ip_address=None,
				public_ipv4_address="203.0.113.7",
				private_ipv4_address="10.0.0.7",
				port=51820,
				private_network_mac_address="aa:bb:cc:dd:ee:07",
			)
		]

		with patch("atlas.metal_server.usage.frappe.get_all", return_value=rows):
			self.assertEqual(get_wireguard_peers(), [])

	def test_wireguard_peers_skip_a_server_without_a_mac_address(self) -> None:
		"""The mesh learns a location only from a peer MAC, so such a peer would stay unreachable."""
		rows = [
			frappe._dict(
				name="server-7",
				wireguard_public_key="key-1",
				wireguard_ip_address="fdab:1::7",
				public_ipv4_address="203.0.113.7",
				private_ipv4_address="10.0.0.7",
				port=51820,
				private_network_mac_address=None,
			)
		]

		with patch("atlas.metal_server.usage.frappe.get_all", return_value=rows):
			self.assertEqual(get_wireguard_peers(), [])

	def test_sync_stores_the_reported_private_network_mac_address(self) -> None:
		server = Mock(private_network_mac_address=None)
		server.name = "server-1"
		client = Mock()
		client.sync.return_value = {"capacity": CAPACITY, "private_network_mac_address": "aa:bb:cc:dd:ee:07"}

		with (
			patch("atlas.metal_server.usage.frappe.get_doc", return_value=server),
			patch("atlas.metal_server.usage.MetalClient", return_value=client),
			patch("atlas.metal_server.usage.get_desired_images", return_value=[]),
			patch("atlas.metal_server.usage.store_reported_states"),
			patch("atlas.metal_server.usage.get_usage_values", return_value={}),
			patch("atlas.metal_server.usage.frappe.db.set_value") as set_value,
		):
			sync_server("server-1", [], [], False)

		set_value.assert_called_once_with(
			"Metal Server", "server-1", "private_network_mac_address", "aa:bb:cc:dd:ee:07"
		)

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
