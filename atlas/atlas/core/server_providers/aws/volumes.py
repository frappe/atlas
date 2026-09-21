from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import frappe
from botocore.exceptions import ClientError

from atlas.atlas.core.server_providers.aws.client import AwsError
from atlas.atlas.core.server_providers.aws.configuration import STORAGE_VOLUME_DEVICE_NAME
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask

if TYPE_CHECKING:
	from atlas.atlas.core.server_providers.aws.provider import AwsProvider
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


class AwsVolumes:
	"""Resize the EBS volumes of an Atlas server and grow them on the host."""

	def __init__(self, provider: "AwsProvider") -> None:
		self.provider = provider

	def volume_id(self, server: "MetalServer", kind: str) -> str:
		"""Return the ID of the root or the storage pool volume."""
		metadata = frappe.parse_json(server.provider_metadata or "{}")
		instance = metadata.get("instance") if isinstance(metadata, Mapping) else None
		instance = instance if isinstance(instance, Mapping) else {}
		device_name = instance.get("RootDeviceName") if kind == "root" else STORAGE_VOLUME_DEVICE_NAME
		for mapping in instance.get("BlockDeviceMappings") or []:
			if isinstance(mapping, Mapping) and mapping.get("DeviceName") == device_name:
				volume_id = (mapping.get("Ebs") or {}).get("VolumeId")
				if volume_id:
					return volume_id
		raise AwsError(f"Atlas server {server.name} has no AWS {kind} volume")

	def describe(self, server: "MetalServer", kind: str) -> dict:
		"""Return the current size, performance, and latest change of one volume."""
		volume_id = self.volume_id(server, kind)
		volume = self.provider.client.call("ec2", "describe_volumes", VolumeIds=[volume_id])["Volumes"][0]
		modifications = self.provider.client.call(
			"ec2", "describe_volumes_modifications", allow_missing=True, VolumeIds=[volume_id]
		).get("VolumesModifications", [])
		latest = max(modifications, key=lambda item: item["StartTime"], default={})
		return {
			"volume_id": volume_id,
			"volume_type": volume["VolumeType"],
			"size_gib": volume["Size"],
			"iops": volume.get("Iops"),
			"throughput_mibps": volume.get("Throughput"),
			"modification_state": latest.get("ModificationState"),
			"last_modified_at": f"{latest['StartTime']:%Y-%m-%d %H:%M} UTC" if latest else None,
		}

	def modify(
		self, server: "MetalServer", kind: str, size_gib: int, iops: int, throughput_mibps: int
	) -> None:
		"""Send the changed values. AWS refuses a smaller size and a second change within 6 hours."""
		current = self.describe(server, kind)
		changes = {
			key: value
			for key, value, field in (
				("Size", size_gib, "size_gib"),
				("Iops", iops, "iops"),
				("Throughput", throughput_mibps, "throughput_mibps"),
			)
			if value and value != current[field]
		}
		if not changes:
			return
		try:
			self.provider.client.call("ec2", "modify_volume", VolumeId=current["volume_id"], **changes)
		except AwsError as error:
			if isinstance(error.__cause__, ClientError):
				raise AwsError(error.__cause__.response["Error"].get("Message") or str(error)) from error
			raise

	def grow(self, server: "MetalServer", kind: str) -> None:
		"""Wait until the guest sees the new size, then grow the file system or the pool."""
		self.provider.poll(
			lambda: self.is_ready(server, kind) or None,
			timeout_seconds=1_800,
			poll_interval_seconds=15,
			description=f"the {kind} volume of server {server.name}",
		)
		environment = {"TARGET": kind}
		if kind == "storage":
			environment["STORAGE_POOL_DEVICE"] = self.provider.storage_pool_device(server)
		task = SSHTask.create_for_script_file(
			target_type=server.doctype,
			target=server.name,
			script_path="aws/grow-disk.sh",
			environment=environment,
			timeout_seconds=600,
			run_in_background=False,
		)
		if not task.result or not task.result.is_success:
			raise AwsError(
				f"Could not grow the {kind} volume of server {server.name}. See SSH Task {task.name}."
			)
		server.enqueue_disk_sync()

	def is_ready(self, server: "MetalServer", kind: str) -> bool:
		"""Report whether the change left the modifying state, which is when the guest sees the new size."""
		if (state := self.describe(server, kind)["modification_state"]) == "failed":
			raise AwsError(f"AWS could not change the {kind} volume of server {server.name}")
		return state != "modifying"
