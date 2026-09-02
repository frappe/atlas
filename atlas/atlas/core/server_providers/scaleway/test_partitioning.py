from __future__ import annotations

from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.scaleway.partitioning import ScalewayPartitioning

_NVME_DEFAULT = {
	"disks": [
		{"device": "/dev/nvme0n1", "partitions": [{"label": "uefi", "number": 1}]},
		{"device": "/dev/nvme1n1", "partitions": [{"label": "uefi", "number": 1}]},
	],
	"raids": [],
	"filesystems": [],
}

# Elastic Metal offers such as EM-A116X-SSD boot with legacy BIOS and have no EFI partition.
_LEGACY_DEFAULT = {
	"disks": [
		{
			"device": "/dev/sda",
			"partitions": [{"label": "legacy", "number": 1}, {"label": "root", "number": 4}],
		},
		{"device": "/dev/sdb", "partitions": [{"label": "root", "number": 3}]},
	],
	"raids": [],
	"filesystems": [],
}


class TestScalewayPartitioning(UnitTestCase):
	def test_schema_mirrors_both_disks_with_the_same_table(self) -> None:
		schema = ScalewayPartitioning().get_schema(_NVME_DEFAULT)

		devices = [disk["device"] for disk in schema["disks"]]
		self.assertEqual(devices, ["/dev/nvme0n1", "/dev/nvme1n1"])
		first_table, second_table = (disk["partitions"] for disk in schema["disks"])
		self.assertEqual(first_table, second_table)
		self.assertEqual([partition["label"] for partition in first_table], ["uefi", "boot", "root", "data"])
		self.assertTrue(first_table[-1]["use_all_available_space"])

	def test_schema_keeps_the_storage_array_out_of_the_filesystems(self) -> None:
		schema = ScalewayPartitioning().get_schema(_NVME_DEFAULT)

		arrays = [raid["name"] for raid in schema["raids"]]
		self.assertEqual(arrays, ["/dev/md0", "/dev/md1", "/dev/md2"])
		self.assertTrue(all(raid["level"] == "raid_level_1" for raid in schema["raids"]))
		formatted = [filesystem["device"] for filesystem in schema["filesystems"]]
		self.assertNotIn("/dev/md2", formatted)
		self.assertEqual(formatted, ["/dev/nvme0n1p1", "/dev/md0", "/dev/md1"])

	def test_schema_pairs_the_matching_partition_of_each_disk(self) -> None:
		schema = ScalewayPartitioning().get_schema(_NVME_DEFAULT)

		self.assertEqual(schema["raids"][2]["devices"], ["/dev/nvme0n1p4", "/dev/nvme1n1p4"])

	def test_schema_names_sata_partitions_without_a_separator(self) -> None:
		default = {"disks": [{"device": "/dev/sda"}, {"device": "/dev/sdb"}]}

		schema = ScalewayPartitioning().get_schema(default)

		self.assertEqual(schema["raids"][2]["devices"], ["/dev/sda4", "/dev/sdb4"])

	def test_schema_keeps_the_legacy_boot_partition(self) -> None:
		schema = ScalewayPartitioning().get_schema(_LEGACY_DEFAULT)

		for table in (disk["partitions"] for disk in schema["disks"]):
			self.assertEqual([partition["label"] for partition in table], ["legacy", "boot", "root", "data"])

	def test_schema_formats_no_efi_partition_on_a_legacy_server(self) -> None:
		schema = ScalewayPartitioning().get_schema(_LEGACY_DEFAULT)

		mountpoints = [filesystem["mountpoint"] for filesystem in schema["filesystems"]]
		self.assertEqual(mountpoints, ["/boot", "/"])

	def test_schema_assumes_uefi_without_a_firmware_partition(self) -> None:
		default = {"disks": [{"device": "/dev/sda", "partitions": []}, {"device": "/dev/sdb"}]}

		schema = ScalewayPartitioning().get_schema(default)

		self.assertEqual(schema["disks"][0]["partitions"][0]["label"], "uefi")
		self.assertEqual(schema["filesystems"][0]["mountpoint"], "/boot/efi")

	def test_schema_is_none_without_a_mirror_pair(self) -> None:
		for default in ({}, {"disks": []}, {"disks": [{"device": "/dev/nvme0n1"}]}):
			with self.subTest(default=default):
				self.assertIsNone(ScalewayPartitioning().get_schema(default))
