from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from botocore.exceptions import ClientError
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.aws.client import AwsError
from atlas.atlas.core.server_providers.aws.volumes import AwsVolumes

VOLUME_ID = "vol-012ac512cc4f05420"


class TestAwsVolumes(UnitTestCase):
	def test_each_kind_finds_its_own_volume(self) -> None:
		volumes = self.volumes()

		self.assertEqual(volumes.volume_id(self.server(), "root"), "vol-0c0bcb91d7b5a82eb")
		self.assertEqual(volumes.volume_id(self.server(), "storage"), VOLUME_ID)

	def test_the_latest_change_is_reported(self) -> None:
		volumes = self.volumes(
			modifications=[
				{"StartTime": datetime(2026, 9, 2, 8, 30, tzinfo=UTC), "ModificationState": "optimizing"},
				{"StartTime": datetime(2026, 9, 1, tzinfo=UTC), "ModificationState": "completed"},
			]
		)

		volume = volumes.describe(self.server(), "storage")

		self.assertEqual(volume["modification_state"], "optimizing")
		self.assertEqual(volume["last_modified_at"], "2026-09-02 08:30 UTC")

	def test_modify_sends_only_the_changed_values(self) -> None:
		volumes = self.volumes()

		volumes.modify(self.server(), "storage", 600, 3000, 250)

		volumes.provider.client.call.assert_called_with(
			"ec2", "modify_volume", VolumeId=VOLUME_ID, Size=600, Throughput=250
		)

	def test_an_unchanged_request_sends_nothing(self) -> None:
		volumes = self.volumes()

		volumes.modify(self.server(), "storage", 500, 3000, 125)

		self.assertNotIn(
			"modify_volume", [call.args[1] for call in volumes.provider.client.call.call_args_list]
		)

	def test_the_aws_message_explains_a_refused_change(self) -> None:
		volumes = self.volumes()
		cause = ClientError(
			{"Error": {"Code": "VolumeModificationRateExceeded", "Message": "Wait 6 hours"}}, ""
		)
		responses = volumes.provider.client.call.side_effect

		def call(service: str, operation: str, **parameters: object) -> dict:
			if operation == "modify_volume":
				raise AwsError("ec2.modify_volume failed") from cause
			return responses(service, operation, **parameters)

		volumes.provider.client.call.side_effect = call

		with self.assertRaisesRegex(AwsError, "Wait 6 hours"):
			volumes.modify(self.server(), "storage", 600, 0, 0)

	def test_grow_runs_the_script_for_the_storage_pool(self) -> None:
		volumes = self.volumes(
			modifications=[{"StartTime": datetime.now(UTC), "ModificationState": "optimizing"}]
		)
		volumes.provider.storage_pool_device.return_value = "/dev/disk/by-id/pool"
		volumes.provider.poll.side_effect = lambda operation, **_: operation()
		server = self.server()
		task = SimpleNamespace(name="SSH-1", result=SimpleNamespace(is_success=True))

		with patch(
			"atlas.atlas.core.server_providers.aws.volumes.SSHTask.create_for_script_file", return_value=task
		) as create:
			volumes.grow(server, "storage")

		self.assertEqual(
			create.call_args.kwargs["environment"],
			{"TARGET": "storage", "STORAGE_POOL_DEVICE": "/dev/disk/by-id/pool"},
		)
		server.enqueue_disk_sync.assert_called_once_with()

	def test_a_failed_change_stops_the_growth(self) -> None:
		volumes = self.volumes(
			modifications=[{"StartTime": datetime.now(UTC), "ModificationState": "failed"}]
		)

		with self.assertRaisesRegex(AwsError, "could not change"):
			volumes.is_ready(self.server(), "root")

	@staticmethod
	def volumes(modifications: list[dict] | None = None) -> AwsVolumes:
		def call(service: str, operation: str, **parameters: object) -> dict:
			if operation == "describe_volumes":
				return {"Volumes": [{"VolumeType": "gp3", "Size": 500, "Iops": 3000, "Throughput": 125}]}
			if operation == "describe_volumes_modifications":
				return {"VolumesModifications": modifications or []}
			return {}

		provider = Mock()
		provider.client.call.side_effect = call
		return AwsVolumes(provider)

	@staticmethod
	def server() -> SimpleNamespace:
		return SimpleNamespace(
			doctype="Metal Server",
			name="server-1",
			enqueue_disk_sync=Mock(),
			provider_metadata=frappe.as_json(
				{
					"instance": {
						"RootDeviceName": "/dev/sda1",
						"BlockDeviceMappings": [
							{"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-0c0bcb91d7b5a82eb"}},
							{"DeviceName": "/dev/sdb", "Ebs": {"VolumeId": VOLUME_ID}},
						],
					}
				}
			),
		)
