from __future__ import annotations

from collections.abc import Mapping


class ScalewayPartitioning:
	"""Build the Scaleway partitioning schema for Atlas."""

	firmware_size_bytes = 512 * 1024**2
	boot_size_bytes = 1024**3
	root_size_bytes = 64 * 1024**3
	raid_level = "raid_level_1"
	boot_array = "/dev/md0"
	root_array = "/dev/md1"
	storage_array = "/dev/md2"
	mirror_disk_count = 2

	def get_schema(self, default_schema: Mapping) -> dict | None:
		"""Mirror both disks and leave the storage array raw.

		Return None when the vendor layout must stay unchanged. A server that boots
		with legacy BIOS has no EFI system partition and rejects one, so the firmware
		partition and the EFI filesystem follow the labels in the vendor layout.
		"""
		disks = [
			disk
			for disk in default_schema.get("disks") or []
			if isinstance(disk, Mapping) and isinstance(disk.get("device"), str)
		]
		if len(disks) < self.mirror_disk_count:
			return None

		first_disk, second_disk = disks[0]["device"], disks[1]["device"]
		labels = {
			partition.get("label")
			for partition in disks[0].get("partitions") or []
			if isinstance(partition, Mapping)
		}
		firmware_label = "legacy" if "legacy" in labels else "uefi"

		filesystems = [
			{"device": self.boot_array, "format": "ext4", "mountpoint": "/boot"},
			{"device": self.root_array, "format": "ext4", "mountpoint": "/"},
		]
		if firmware_label == "uefi":
			efi = {"device": self._partition(first_disk, 1), "format": "fat32", "mountpoint": "/boot/efi"}
			filesystems.insert(0, efi)

		return {
			"disks": [self._disk(first_disk, firmware_label), self._disk(second_disk, firmware_label)],
			"raids": [
				self._raid(self.boot_array, first_disk, second_disk, 2),
				self._raid(self.root_array, first_disk, second_disk, 3),
				self._raid(self.storage_array, first_disk, second_disk, 4),
			],
			"filesystems": filesystems,
		}

	def _disk(self, device: str, firmware_label: str) -> dict:
		return {
			"device": device,
			"partitions": [
				{"label": firmware_label, "number": 1, "size": self.firmware_size_bytes},
				{"label": "boot", "number": 2, "size": self.boot_size_bytes},
				{"label": "root", "number": 3, "size": self.root_size_bytes},
				{"label": "data", "number": 4, "use_all_available_space": True},
			],
		}

	def _raid(self, name: str, first_disk: str, second_disk: str, number: int) -> dict:
		return {
			"name": name,
			"level": self.raid_level,
			"devices": [self._partition(first_disk, number), self._partition(second_disk, number)],
		}

	@staticmethod
	def _partition(device: str, number: int) -> str:
		"""Return the partition path for a device."""
		separator = "p" if device[-1].isdigit() else ""
		return f"{device}{separator}{number}"
