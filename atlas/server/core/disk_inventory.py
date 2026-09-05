from __future__ import annotations

import json
from collections import deque
from typing import TYPE_CHECKING

import frappe
from frappe import _

from atlas.server.doctype.server_ssh_task.server_ssh_task import ServerSSHTask

if TYPE_CHECKING:
	from atlas.server.doctype.server.server import Server


class DiskInventory:
	"""Read and parse the block devices for one server."""

	def __init__(self, server: "Server") -> None:
		self.server = server

	def sync(self) -> None:
		"""Replace the Server disk rows with the current host inventory."""
		result = ServerSSHTask.create_for_command(
			server=self.server.name,
			command="lsblk --json --bytes --paths --output NAME,UUID,SIZE,MOUNTPOINT",
			run_in_background=False,
		).result
		if not result or not result.is_success:
			frappe.throw(_("Could not read the disks of server {0}.").format(self.server.name))

		self.server.set("disks", self.parse(result.output))
		self.server.save()

	def parse(self, lsblk_output: str) -> list[dict[str, str]]:
		"""Return mounted devices and the raw storage pool device."""
		storage_pool_device = self.server.settings.server_provider_controller.get_storage_pool_device(
			self.server
		)
		devices = deque(json.loads(lsblk_output).get("blockdevices", []))
		disks: dict[str, dict[str, str]] = {}
		while devices:
			device = devices.popleft()
			devices.extendleft(reversed(device.get("children") or []))
			name = device["name"]
			if not device.get("mountpoint") and name != storage_pool_device:
				continue
			disks[name] = {
				"device": name,
				"uuid": device.get("uuid") or "",
				"mount_point": device.get("mountpoint") or "",
				"size_gb": f"{int(device.get('size') or 0) / 1024**3:.2f}",
			}
		return list(disks.values())
