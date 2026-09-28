from __future__ import annotations

import json
import pathlib
from datetime import datetime
from unittest.mock import patch

from frappe.tests import UnitTestCase

from atlas.vm.core.metal_models import MetalVirtualMachine, timestamp_field

SWAGGER_PATH = pathlib.Path(__file__).parents[3] / "metal" / "internal" / "api" / "swagger.json"


def complete_response() -> dict:
	return {
		"id": "VM-00001",
		"desired": {
			"generation": 3,
			"restart_generation": 1,
			"state": "running",
			"disk_encryption": "luks2",
			"compute": {
				"cpu_millicores": 2500,
				"memory_mib": 2048,
				"sleep_after_idle_seconds": 1800,
			},
			"disk": {"size_mib": 8192, "throughput_mibps": 100, "iops": 500},
			"image": {
				"ref": "ubuntu",
				"architecture": "amd64",
				"rootfs": {"sha256": "a" * 64},
				"kernel": {"sha256": "b" * 64},
				"initrd": {"sha256": "c" * 64},
				"cache_image": True,
				"memory_snapshot": True,
				"memory_snapshot_configuration": {
					"virtual_cpu_count": 2,
					"memory_mib": 2048,
					"disk_mib": 8192,
				},
			},
			"network": {
				"routes": [{"destination": "0.0.0.0/0", "via": "host"}],
				"public_ipv4": "1.2.3.4",
				"wireguard_mesh_ipv6": "fdaa::1",
				"private_network_throughput_mibps": 10,
				"public_network_throughput_mibps": 20,
				"firewall": {
					"enabled": True,
					"inbound": [{"protocol": "tcp", "ports": "22", "cidrs": ["203.0.113.0/24"]}],
					"outbound": [{"protocol": "any", "cidrs": ["0.0.0.0/0", "::/0"]}],
				},
			},
			"guest": {"hostname": "web", "ssh_keys": ["ssh-ed25519 AAAA"], "metadata": {"role": "web"}},
		},
		"observed": {
			"generation": 3,
			"restart_generation": 1,
			"state": "running",
			"updated_at": "2026-09-06T10:00:00Z",
			"disk": {"used_mib": 1024},
			"network": {"mac": "06:00:ac:10:00:02"},
			"error": {
				"code": "operation_failed",
				"message": "network failed",
				"updated_at": "2026-09-06T10:00:00Z",
			},
		},
	}


def new_virtual_machine_response() -> dict:
	"""Return what Metal sends for a VM it has accepted but not yet reconciled."""
	return {
		"id": "VM-00002",
		"desired": {
			"generation": 1,
			"restart_generation": 0,
			"state": "running",
			"compute": {
				"cpu_millicores": 0,
				"memory_mib": 0,
				"sleep_after_idle_seconds": 0,
			},
			"disk": {"size_mib": 0, "throughput_mibps": 0, "iops": 0},
			"image": {
				"ref": "ubuntu",
				"architecture": "amd64",
				"rootfs": {"sha256": "a" * 64},
				"kernel": {"sha256": "b" * 64},
				"cache_image": False,
				"memory_snapshot": False,
			},
			"network": {
				"routes": [],
				"wireguard_mesh_ipv6": "",
				"private_network_throughput_mibps": 0,
				"public_network_throughput_mibps": 0,
				"firewall": {"enabled": False, "inbound": [], "outbound": []},
			},
			"guest": {"hostname": "", "ssh_keys": [], "metadata": {}},
		},
		"observed": {
			"generation": 0,
			"restart_generation": 0,
			"state": "unknown",
			"updated_at": "2026-09-06T10:00:00Z",
			"disk": {"used_mib": 0},
			"network": {},
		},
	}


class TestMetalVirtualMachineParsing(UnitTestCase):
	def test_a_complete_response_parses_into_typed_values(self) -> None:
		machine = MetalVirtualMachine.from_dict(complete_response())

		self.assertEqual(machine.id, "VM-00001")
		self.assertEqual(machine.desired.compute.cpu_millicores, 2500)
		self.assertEqual(machine.desired.compute.sleep_after_idle_seconds, 1800)
		self.assertEqual(machine.desired.disk_encryption, "luks2")
		self.assertEqual(machine.desired.image.rootfs.sha256, "a" * 64)
		self.assertEqual(machine.desired.image.initrd.sha256, "c" * 64)
		self.assertEqual(machine.desired.guest.ssh_keys, ("ssh-ed25519 AAAA",))
		self.assertEqual(machine.desired.guest.metadata, {"role": "web"})
		self.assertTrue(machine.desired.network.firewall.enabled)
		self.assertEqual(machine.desired.network.firewall.inbound[0].ports, "22")
		self.assertEqual(machine.observed.network.mac, "06:00:ac:10:00:02")
		self.assertEqual(machine.observed.error.message, "network failed")

	def test_a_new_virtual_machine_parses_with_empty_values(self) -> None:
		"""Metal sends every group for a VM it has not reconciled yet."""
		machine = MetalVirtualMachine.from_dict(new_virtual_machine_response())

		self.assertEqual(machine.desired.state, "running")
		self.assertEqual(machine.desired.compute.cpu_millicores, 0)
		self.assertEqual(machine.desired.disk_encryption, "")
		self.assertIsNone(machine.desired.image.initrd)
		self.assertEqual(machine.desired.guest.ssh_keys, ())
		self.assertEqual(machine.observed.state, "unknown")
		self.assertIsNone(machine.observed.error)

	def test_an_omitted_public_ipv4_is_tolerated(self) -> None:
		"""Metal omits public_ipv4 when no address is attached."""
		response = new_virtual_machine_response()
		self.assertNotIn("public_ipv4", response["desired"]["network"])

		machine = MetalVirtualMachine.from_dict(response)

		self.assertEqual(machine.desired.network.public_ipv4, "")

	def test_a_response_missing_a_required_group_is_rejected(self) -> None:
		"""A truncated response must fail loudly, not read as an empty VM."""
		for removed in ("desired", "observed"):
			response = new_virtual_machine_response()
			del response[removed]
			with self.assertRaises(ValueError):
				MetalVirtualMachine.from_dict(response)

	def test_an_absent_memory_snapshot_configuration_stays_none(self) -> None:
		response = complete_response()
		del response["desired"]["image"]["memory_snapshot_configuration"]

		machine = MetalVirtualMachine.from_dict(response)

		self.assertIsNone(machine.desired.image.memory_snapshot_configuration)

	def test_as_dict_is_json_compatible(self) -> None:
		"""as_dict feeds a JSON response, so it must survive an encode and decode."""
		machine = MetalVirtualMachine.from_dict(complete_response())

		decoded = json.loads(json.dumps(machine.as_dict()))

		self.assertEqual(MetalVirtualMachine.from_dict(decoded), machine)


class TestMetalTimestamps(UnitTestCase):
	def test_converts_a_nanosecond_utc_timestamp_to_the_system_timezone(self) -> None:
		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			converted = timestamp_field({"started_at": "2026-09-20T18:55:43.02675576Z"}, "started_at")

		self.assertEqual(converted, datetime(2026, 9, 21, 0, 25, 43, 26755))
		self.assertIsNone(converted.tzinfo)

	def test_reads_an_unset_go_time_as_no_value(self) -> None:
		self.assertIsNone(timestamp_field({"finished_at": "0001-01-01T00:00:00Z"}, "finished_at"))
		self.assertIsNone(timestamp_field({"finished_at": ""}, "finished_at"))
		self.assertIsNone(timestamp_field({}, "finished_at"))

	def test_rejects_a_timestamp_that_is_not_a_string(self) -> None:
		with self.assertRaises(ValueError):
			timestamp_field({"started_at": 1758393343}, "started_at")


class TestMetalContract(UnitTestCase):
	"""Fail when Metal's published response shape stops matching what Atlas reads."""

	def load_definitions(self) -> dict:
		if not SWAGGER_PATH.exists():
			self.skipTest(f"Metal OpenAPI document not found at {SWAGGER_PATH}")
		return json.loads(SWAGGER_PATH.read_text())["definitions"]

	def assert_has_fields(self, definitions: dict, name: str, fields: set[str]) -> None:
		published = set(definitions[name]["properties"])
		self.assertTrue(
			fields <= published,
			f"{name} no longer publishes {sorted(fields - published)}",
		)

	def test_metal_publishes_every_field_atlas_reads(self) -> None:
		definitions = self.load_definitions()

		self.assert_has_fields(
			definitions,
			"api.desiredVirtualMachineResponse",
			{
				"generation",
				"restart_generation",
				"state",
				"disk_encryption",
				"compute",
				"disk",
				"image",
				"network",
				"guest",
			},
		)
		self.assert_has_fields(definitions, "api.createRequest", {"disk_encryption"})
		self.assert_has_fields(definitions, "api.imageRequest", {"initrd"})
		self.assert_has_fields(
			definitions,
			"api.computeResponse",
			{"cpu_millicores", "memory_mib", "sleep_after_idle_seconds"},
		)
		self.assert_has_fields(
			definitions,
			"api.observedVirtualMachineResponse",
			{"generation", "restart_generation", "state", "updated_at", "disk", "network", "error"},
		)
		self.assert_has_fields(definitions, "api.observedDiskResponse", {"used_mib"})
		self.assert_has_fields(definitions, "api.operationErrorResponse", {"code", "message", "updated_at"})
		self.assert_has_fields(definitions, "api.virtualMachineResponse", {"id", "desired", "observed"})
		self.assert_has_fields(
			definitions,
			"api.virtualMachineImageResponse",
			{"ref", "architecture", "rootfs", "kernel", "initrd", "cache_image", "memory_snapshot"},
		)

	def test_metal_still_serves_the_routes_atlas_calls(self) -> None:
		if not SWAGGER_PATH.exists():
			self.skipTest(f"Metal OpenAPI document not found at {SWAGGER_PATH}")
		paths = set(json.loads(SWAGGER_PATH.read_text())["paths"])

		expected = {
			"/v1/sync",
			"/v1/vms/{id}",
			"/v1/vms/{id}/power",
			"/v1/vms/{id}/restart",
			"/v1/vms/{id}/compute",
			"/v1/vms/{id}/disk",
			"/v1/vms/{id}/network",
			"/v1/vms/{id}/ssh-keys",
			"/v1/vms/{id}/metadata",
			"/v1/vms/{id}/snapshots",
			"/v1/snapshots/{id}",
			"/v1/snapshots/{id}/upload",
		}
		self.assertTrue(expected <= paths, f"Metal no longer serves {sorted(expected - paths)}")
