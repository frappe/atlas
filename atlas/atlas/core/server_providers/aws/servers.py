from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import ClassVar

from atlas.atlas.core.server_providers.aws.catalog import AwsCatalog
from atlas.atlas.core.server_providers.aws.client import AwsClient, AwsError
from atlas.atlas.core.server_providers.aws.configuration import (
	ROOT_VOLUME_SIZE_GIB,
	STORAGE_VOLUME_DEVICE_NAME,
	STORAGE_VOLUME_SIZE_GIB,
	AwsConfiguration,
)
from atlas.atlas.core.server_providers.base import (
	ProviderServer,
	ServerCreateRequest,
	ServerPowerAction,
)


class AwsServers:
	"""Own AWS Elastic Compute Cloud instance operations."""

	server_status_map: ClassVar[dict[str, str]] = {
		"pending": "Installing",
		"running": "Installing",
		"stopping": "Stopped",
		"stopped": "Stopped",
		"shutting-down": "Deleted",
		"terminated": "Deleted",
	}
	power_operation_map: ClassVar[dict[ServerPowerAction, str]] = {
		ServerPowerAction.REBOOT: "reboot_instances",
		ServerPowerAction.START: "start_instances",
		ServerPowerAction.STOP: "stop_instances",
	}
	live_states: ClassVar[frozenset[str]] = frozenset({"pending", "running", "stopping", "stopped"})
	identity_tag_key: ClassVar[str] = "atlas-server"

	def __init__(self, client: AwsClient, configuration: AwsConfiguration, catalog: AwsCatalog) -> None:
		self.client = client
		self.configuration = configuration
		self.catalog = catalog

	def ensure(self, request: ServerCreateRequest) -> ProviderServer:
		"""Return the named instance, and create it when it does not exist."""
		instance = self.find(request.name)
		if instance is None:
			instance = self.create(request)

		return self.to_provider_server(instance)

	def create(self, request: ServerCreateRequest) -> Mapping:
		"""Create one AWS instance from an Atlas request."""
		if (
			not self.configuration.key_pair_name
			or not self.configuration.subnet_id
			or not self.configuration.security_group_id
		):
			raise AwsError("Atlas Settings has no AWS key pair, subnet, or security group")

		response = self.client.call(
			"ec2",
			"run_instances",
			ClientToken=self.client_token("instance", request.name),
			ImageId=self.catalog.image_id(request.image_provider_metadata, request.server_image),
			InstanceType=request.server_size,
			MinCount=1,
			MaxCount=1,
			KeyName=self.configuration.key_pair_name,
			SubnetId=self.configuration.subnet_id,
			SecurityGroupIds=[self.configuration.security_group_id],
			BlockDeviceMappings=[self.root_volume(request), self.storage_volume()],
			TagSpecifications=[
				{
					"ResourceType": "instance",
					"Tags": [
						{"Key": "Name", "Value": request.name},
						{"Key": self.identity_tag_key, "Value": request.name},
					],
				}
			],
			**self.cpu_options(request.size_provider_metadata),
		)
		instances = response.get("Instances", [])
		if not isinstance(instances, list) or len(instances) != 1 or not isinstance(instances[0], Mapping):
			raise AwsError(f"AWS did not return an instance for Atlas server {request.name}")
		return instances[0]

	def root_volume(self, request: ServerCreateRequest) -> dict:
		"""Return the resized volume mapping for the image root device."""
		return {
			"DeviceName": self.catalog.root_device_name(
				request.image_provider_metadata, request.server_image
			),
			"Ebs": {
				"VolumeSize": ROOT_VOLUME_SIZE_GIB,
				"VolumeType": "gp3",
				"DeleteOnTermination": True,
			},
		}

	@staticmethod
	def storage_volume() -> dict:
		"""Return the volume mapping for the virtual machine storage pool."""
		return {
			"DeviceName": STORAGE_VOLUME_DEVICE_NAME,
			"Ebs": {
				"VolumeSize": STORAGE_VOLUME_SIZE_GIB,
				"VolumeType": "gp3",
				"DeleteOnTermination": True,
			},
		}

	@staticmethod
	def cpu_options(size_provider_metadata: Mapping) -> dict:
		"""Return nested virtualization options for the instance type."""
		if size_provider_metadata.get("BareMetal"):
			return {}
		return {"CpuOptions": {"NestedVirtualization": "enabled"}}

	def find(self, name: str) -> Mapping | None:
		"""Return the instance with the Atlas identity tag."""
		reservations = self.client.paginate(
			"ec2",
			"describe_instances",
			"Reservations",
			Filters=[
				{"Name": f"tag:{self.identity_tag_key}", "Values": [name]},
				{"Name": "instance-state-name", "Values": sorted(self.live_states)},
			],
		)
		instances = [instance for reservation in reservations for instance in reservation["Instances"]]
		if len(instances) > 1:
			raise AwsError(f"AWS returned multiple instances for Atlas server {name}")
		return instances[0] if instances else None

	def fetch(self, provider_server_id: str) -> Mapping:
		"""Return one AWS instance."""
		response = self.client.call("ec2", "describe_instances", InstanceIds=[provider_server_id])
		for reservation in response.get("Reservations", []):
			for instance in reservation.get("Instances", []):
				return instance
		raise AwsError(f"AWS has no instance {provider_server_id}")

	def is_ready(self, provider_server_id: str) -> bool:
		"""Report whether both AWS status checks pass for one instance."""
		response = self.client.call("ec2", "describe_instance_status", InstanceIds=[provider_server_id])
		for status in response.get("InstanceStatuses", []):
			return (
				status.get("InstanceStatus", {}).get("Status") == "ok"
				and status.get("SystemStatus", {}).get("Status") == "ok"
			)
		return False

	@staticmethod
	def client_token(kind: str, server_name: str) -> str:
		"""Return a stable AWS idempotency token for one server resource."""
		return hashlib.sha256(f"atlas:{kind}:{server_name}".encode()).hexdigest()

	def set_power_state(self, provider_server_id: str, action: ServerPowerAction) -> None:
		"""Apply one power action to an AWS instance."""
		self.client.call("ec2", self.power_operation_map[action], InstanceIds=[provider_server_id])

	def delete(self, provider_server_id: str) -> None:
		"""Delete one AWS instance if it exists."""
		self.client.call("ec2", "terminate_instances", InstanceIds=[provider_server_id], allow_missing=True)

	@classmethod
	def to_provider_server(cls, instance: Mapping) -> ProviderServer:
		"""Convert one AWS instance to provider-neutral data."""
		provider_server_id = instance.get("InstanceId")
		if not isinstance(provider_server_id, str):
			raise AwsError("AWS did not return an instance ID")

		public_address = instance.get("PublicIpAddress")
		return ProviderServer(
			provider_server_id=provider_server_id,
			status=cls.server_status_map.get(instance.get("State", {}).get("Name")),
			public_ipv4_address=public_address if isinstance(public_address, str) else None,
			provider_metadata={"instance": dict(instance)},
		)
