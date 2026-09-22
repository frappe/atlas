from __future__ import annotations

import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.metal_server.core.host_inspection import (
	DISK_IMAGE_PATH,
	REPORT_END,
	REPORT_START,
	HostInspection,
	HostReport,
	HostReportError,
	ensure_server_size,
	validate_addresses,
)

PUBLIC_ADDRESS = "203.0.113.5"
PRIVATE_ADDRESS = "10.0.0.5"
MODULE = "atlas.metal_server.core.host_inspection"


def probe_output(**overrides: object) -> str:
	report = {
		"hostname": "rack-1-host-1",
		"machine": "x86_64",
		"os_id": "ubuntu",
		"os_version": "24.04",
		"cpu_count": 32,
		"memory_bytes": 128 * 1024**3,
		"has_kvm": True,
		"image_available_bytes": 200 * 1024**3,
		"disk_image": None,
		"addresses": [
			{"ifname": "lo", "mtu": 65536, "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
			{"ifname": "vxlan0", "mtu": 1450, "addr_info": [{"family": "inet", "local": PRIVATE_ADDRESS}]},
			{"ifname": "eno1", "mtu": 1500, "addr_info": [{"family": "inet", "local": "192.168.0.10"}]},
		],
		"default_routes": [{"dst": "default", "gateway": "192.168.0.1", "dev": "eno1"}],
		"block_devices": {
			"blockdevices": [
				{"name": "/dev/loop0", "type": "loop", "size": 1024**3, "fstype": "squashfs"},
				{"name": "/dev/zram0", "type": "disk", "size": 1024**3},
				{
					"name": "/dev/nvme0n1",
					"type": "disk",
					"size": 500 * 1024**3,
					"pttype": "gpt",
					"children": [{}],
				},
				{"name": "/dev/nvme1n1", "type": "disk", "size": 1800 * 1024**3, "ro": False, "rm": False},
				{"name": "/dev/sdb", "type": "disk", "size": 8 * 1024**3, "rm": "1"},
			]
		},
		**overrides,
	}
	return f"noise\n{REPORT_START}\n{json.dumps(report)}\n{REPORT_END}\n"


def report(**overrides: object) -> HostReport:
	return replace(HostReport.parse(probe_output(), PUBLIC_ADDRESS, PRIVATE_ADDRESS), **overrides)


class TestHostReport(UnitTestCase):
	def test_report_reads_the_host_facts(self) -> None:
		parsed = HostReport.parse(probe_output(), PUBLIC_ADDRESS, PRIVATE_ADDRESS)

		self.assertEqual(parsed.os, "Ubuntu")
		self.assertEqual(parsed.memory_mib, 128 * 1024)
		self.assertEqual((parsed.private_network_interface, parsed.private_network_mtu), ("vxlan0", 1450))
		self.assertEqual(parsed.architecture, "amd64")
		self.assertEqual(parsed.server_size_name, "32x128")
		self.assertEqual(parsed.server_image_name, "Ubuntu 24.04")

	def test_public_interface_holds_the_public_address(self) -> None:
		addresses = [
			{"ifname": "eno2", "mtu": 1500, "addr_info": [{"family": "inet", "local": PUBLIC_ADDRESS}]},
			{"ifname": "vxlan0", "mtu": 1450, "addr_info": [{"family": "inet", "local": PRIVATE_ADDRESS}]},
		]
		parsed = HostReport.parse(probe_output(addresses=addresses), PUBLIC_ADDRESS, PRIVATE_ADDRESS)

		self.assertEqual(parsed.public_network_interface, "eno2")

	def test_public_interface_falls_back_to_the_default_route(self) -> None:
		parsed = HostReport.parse(probe_output(), PUBLIC_ADDRESS, PRIVATE_ADDRESS)

		self.assertEqual(parsed.public_network_interface, "eno1")

	def test_only_empty_whole_disks_are_free(self) -> None:
		parsed = HostReport.parse(probe_output(), PUBLIC_ADDRESS, PRIVATE_ADDRESS)

		self.assertEqual([disk.device for disk in parsed.disks], ["/dev/nvme0n1", "/dev/nvme1n1", "/dev/sdb"])
		self.assertEqual(parsed.free_devices, ["/dev/nvme1n1"])
		self.assertIn("partition", parsed.disks[0].busy_reason)
		self.assertEqual(parsed.disks[2].busy_reason, "is removable")

	def test_output_without_markers_is_rejected(self) -> None:
		with self.assertRaisesRegex(HostReportError, "no report"):
			HostReport.parse("Permission denied", PUBLIC_ADDRESS, PRIVATE_ADDRESS)

	def test_invalid_report_is_rejected(self) -> None:
		with self.assertRaisesRegex(HostReportError, "invalid report"):
			HostReport.parse(f"{REPORT_START}{{}}{REPORT_END}", PUBLIC_ADDRESS, PRIVATE_ADDRESS)

	def test_a_prepared_host_has_no_failures(self) -> None:
		self.assertEqual(report().get_failures(PRIVATE_ADDRESS), [])

	def test_missing_free_disk_is_not_a_host_failure(self) -> None:
		self.assertEqual(report(disks=()).get_failures(PRIVATE_ADDRESS), [])

	def test_each_unprepared_fact_is_reported(self) -> None:
		cases = {
			"x86_64": {"machine": "aarch64"},
			"operating system": {"os": "Fedora", "os_version": "44"},
			"KVM": {"has_kvm": False},
			"No interface": {"private_network_interface": "", "private_network_mtu": 0},
			"MTU 1280": {"private_network_mtu": 1280},
		}
		for message, overrides in cases.items():
			with self.subTest(message):
				failures = report(**overrides).get_failures(PRIVATE_ADDRESS)
				self.assertEqual(len(failures), 1)
				self.assertIn(message, failures[0])

	def test_disk_image_is_a_disk(self) -> None:
		image = {"name": DISK_IMAGE_PATH, "size": 100 * 1024**3, "fstype": "", "is_file": True}
		parsed = HostReport.parse(probe_output(disk_image=image), PUBLIC_ADDRESS, PRIVATE_ADDRESS)

		self.assertIn(DISK_IMAGE_PATH, parsed.free_devices)
		self.assertFalse(parsed.can_create_disk_image)

	def test_disk_image_with_a_pool_is_busy(self) -> None:
		image = {"name": DISK_IMAGE_PATH, "size": 100 * 1024**3, "fstype": "zfs_member", "is_file": True}
		parsed = HostReport.parse(probe_output(disk_image=image), PUBLIC_ADDRESS, PRIVATE_ADDRESS)

		self.assertNotIn(DISK_IMAGE_PATH, parsed.free_devices)
		self.assertIn("signature", parsed.disks[-1].busy_reason)

	def test_disk_image_is_offered_only_without_a_free_disk(self) -> None:
		self.assertFalse(report().can_create_disk_image)
		self.assertTrue(report(disks=()).can_create_disk_image)
		self.assertFalse(report(disks=(), image_available_gib=0).can_create_disk_image)
		self.assertEqual(report().disk_image_default_size_gib, 160)

	def test_cached_report_round_trips(self) -> None:
		original = report()

		self.assertEqual(HostReport.from_dict(json.loads(json.dumps(asdict(original)))), original)


class TestHostInspection(UnitTestCase):
	def test_run_caches_the_report_and_its_failures(self) -> None:
		inspection = HostInspection("inspection-1")
		result = SimpleNamespace(is_success=True, output=probe_output(machine="aarch64"))

		with (
			patch.object(HostInspection, "state", new_callable=PropertyMock, return_value=self.state()),
			patch.object(inspection, "store") as store,
			patch(f"{MODULE}.SSHRunner") as runner,
		):
			runner.return_value.run_script.return_value = result
			inspection.run()

		stored = store.call_args.args[0]
		self.assertEqual(stored["status"], "Completed")
		self.assertEqual(stored["server_size_name"], "32x128")
		self.assertIn("aarch64", stored["failures"][0])
		runner.assert_called_once_with(PUBLIC_ADDRESS)

	def test_run_caches_an_unreachable_host_as_failed(self) -> None:
		inspection = HostInspection("inspection-1")

		with (
			patch.object(HostInspection, "state", new_callable=PropertyMock, return_value=self.state()),
			patch.object(inspection, "store") as store,
			patch(f"{MODULE}.SSHRunner") as runner,
		):
			runner.return_value.run_script.side_effect = OSError("Connection refused")
			inspection.run()

		self.assertEqual(store.call_args.args[0]["status"], "Failed")
		self.assertIn("Connection refused", store.call_args.args[0]["error"])

	def test_run_offers_a_disk_image_when_no_disk_is_free(self) -> None:
		inspection = HostInspection("inspection-1")
		devices = [{"name": "/dev/nvme0n1", "type": "disk", "size": 1024**3, "pttype": "gpt"}]
		output = probe_output(block_devices={"blockdevices": devices})

		with (
			patch.object(HostInspection, "state", new_callable=PropertyMock, return_value=self.state()),
			patch.object(inspection, "store") as store,
			patch(f"{MODULE}.SSHRunner") as runner,
		):
			runner.return_value.run_script.return_value = SimpleNamespace(is_success=True, output=output)
			inspection.run()

		stored = store.call_args.args[0]
		self.assertEqual(stored["failures"], [])
		self.assertTrue(stored["can_create_disk_image"])
		self.assertEqual(
			runner.return_value.run_script.call_args.kwargs["data"], {"DISK_IMAGE_PATH": DISK_IMAGE_PATH}
		)

	def test_disk_image_size_must_fit_the_free_space(self) -> None:
		state = {**self.completed_state(), "can_create_disk_image": True}

		with (
			patch.object(HostInspection, "state", new_callable=PropertyMock, return_value=state),
			patch(f"{MODULE}.frappe.enqueue") as enqueue,
		):
			for size_gib in (0, 201):
				with self.subTest(size_gib), self.assertRaises(frappe.ValidationError):
					HostInspection("inspection-1").start_disk_image_creation(size_gib)
		enqueue.assert_not_called()

	def test_disk_image_is_refused_when_the_host_has_a_free_disk(self) -> None:
		with (
			patch.object(
				HostInspection, "state", new_callable=PropertyMock, return_value=self.completed_state()
			),
			self.assertRaises(frappe.ValidationError),
		):
			HostInspection("inspection-1").start_disk_image_creation(10)

	def test_failed_disk_image_creation_is_cached_as_failed(self) -> None:
		inspection = HostInspection("inspection-1")

		with (
			patch.object(HostInspection, "state", new_callable=PropertyMock, return_value=self.state()),
			patch.object(inspection, "store") as store,
			patch.object(inspection, "run") as run,
			patch(f"{MODULE}.SSHRunner") as runner,
		):
			runner.return_value.run_script.return_value = SimpleNamespace(
				is_success=False, output="fallocate: No space left on device"
			)
			inspection.create_disk_image(100)

		self.assertEqual(store.call_args.args[0]["status"], "Failed")
		self.assertIn("No space left", store.call_args.args[0]["error"])
		run.assert_not_called()

	def test_created_disk_image_is_inspected_again(self) -> None:
		inspection = HostInspection("inspection-1")

		with (
			patch.object(HostInspection, "state", new_callable=PropertyMock, return_value=self.state()),
			patch.object(inspection, "run") as run,
			patch(f"{MODULE}.SSHRunner") as runner,
		):
			runner.return_value.run_script.return_value = SimpleNamespace(is_success=True, output="")
			inspection.create_disk_image(100)

		run.assert_called_once_with()
		self.assertEqual(runner.return_value.run_script.call_args.kwargs["data"]["DISK_IMAGE_SIZE_GIB"], 100)

	def test_register_refuses_a_disk_that_is_not_free(self) -> None:
		with (
			patch.object(
				HostInspection, "state", new_callable=PropertyMock, return_value=self.completed_state()
			),
			self.assertRaises(frappe.ValidationError),
		):
			HostInspection("inspection-1").register("/dev/nvme0n1", None)

	def test_register_refuses_an_inspection_with_failures(self) -> None:
		state = {**self.completed_state(), "failures": ["The host has no KVM device."]}

		with (
			patch.object(HostInspection, "state", new_callable=PropertyMock, return_value=state),
			self.assertRaises(frappe.ValidationError),
		):
			HostInspection("inspection-1").register("/dev/nvme1n1", None)

	def test_register_inserts_the_server_with_the_host_facts(self) -> None:
		server = MagicMock()

		with (
			patch.object(
				HostInspection, "state", new_callable=PropertyMock, return_value=self.completed_state()
			),
			patch(f"{MODULE}.validate_addresses") as validate,
			patch(f"{MODULE}.ensure_server_size", return_value="32x128") as ensure_size,
			patch(f"{MODULE}.ensure_server_image", return_value="Ubuntu 24.04"),
			patch(f"{MODULE}.frappe.new_doc", return_value=server),
			patch(f"{MODULE}.frappe.db.advisory_lock"),
			patch(f"{MODULE}.frappe.cache.delete_value") as delete_value,
		):
			HostInspection("inspection-1").register("/dev/nvme1n1", "")

		values = server.update.call_args.args[0]
		metadata = json.loads(values["provider_metadata"])
		self.assertRegex(values["provider_server_id"], r"^generic-\d{2}-\d{2}-\d{4}-[a-z0-9]{6}$")
		self.assertEqual(values["title"], "rack-1-host-1")
		self.assertEqual(values["public_network_interface"], "eno1")
		self.assertEqual(values["private_network_interface"], "vxlan0")
		self.assertEqual(values["architecture"], "amd64")
		self.assertEqual(metadata["storage_pool_device"], "/dev/nvme1n1")
		self.assertEqual(metadata["storage_pool_size_gib"], 1800)
		ensure_size.assert_called_once()
		validate.assert_called_once_with(PUBLIC_ADDRESS, PRIVATE_ADDRESS)
		server.insert.assert_called_once_with(ignore_permissions=True)
		delete_value.assert_called_once_with("atlas:host-inspection:inspection-1")

	@staticmethod
	def state() -> dict:
		return {
			"status": "Inspecting",
			"public_ipv4_address": PUBLIC_ADDRESS,
			"private_ipv4_address": PRIVATE_ADDRESS,
		}

	def completed_state(self) -> dict:
		return {**self.state(), "status": "Completed", "report": asdict(report()), "failures": []}


class TestHostRegistrationChecks(UnitTestCase):
	def test_private_address_must_be_inside_the_private_network(self) -> None:
		with self.generic_settings(), self.assertRaisesRegex(frappe.ValidationError, "inside"):
			validate_addresses(PUBLIC_ADDRESS, "192.168.1.5")

	def test_address_of_an_active_server_is_refused(self) -> None:
		with (
			self.generic_settings(),
			patch(f"{MODULE}.frappe.db.exists", return_value="server-1"),
			self.assertRaisesRegex(frappe.ValidationError, "server-1"),
		):
			validate_addresses(PUBLIC_ADDRESS, PRIVATE_ADDRESS)

	def test_existing_size_must_match_the_host(self) -> None:
		existing = SimpleNamespace(architecture="amd64", cpu_count=16)

		with (
			patch(f"{MODULE}.frappe.db.exists", return_value=True),
			patch(f"{MODULE}.frappe.get_doc", return_value=existing),
			self.assertRaisesRegex(frappe.ValidationError, "does not match"),
		):
			ensure_server_size(report(), 1800)

	def test_missing_size_is_created_from_the_host(self) -> None:
		with (
			patch(f"{MODULE}.frappe.db.exists", return_value=False),
			patch(f"{MODULE}.frappe.get_doc") as get_doc,
		):
			name = ensure_server_size(report(), 1800)

		self.assertEqual(name, "32x128")
		values = get_doc.call_args.args[0]
		self.assertEqual((values["cpu_count"], values["memory_mib"], values["disk_gib"]), (32, 131072, 1800))

	@staticmethod
	def generic_settings():
		from atlas.atlas.core.server_providers.generic import GenericProvider

		settings = SimpleNamespace(private_network_cidr="10.0.0.0/16")
		settings.server_provider_controller = Mock(spec=GenericProvider)
		return patch(f"{MODULE}.frappe.get_single", return_value=settings)
