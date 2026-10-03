from __future__ import annotations

import re
from collections.abc import Mapping

from atlas.atlas.core.server_providers.aws.client import AwsError
from atlas.atlas.core.server_providers.aws.configuration import STORAGE_VOLUME_SIZE_GIB
from atlas.atlas.core.server_providers.base import (
	ACCEPTED_OS_VERSIONS,
	ServerImageData,
	ServerSizeData,
)

IMAGE_OWNERS: Mapping[str, str] = {"Ubuntu": "099720109477", "Debian": "136693071363"}

UBUNTU_IMAGE_NAME = re.compile(r"ubuntu/images/hvm-ssd(?:-gp3)?/ubuntu-\w+-(\d\d\.\d\d)-amd64-server-")
DEBIAN_IMAGE_NAME = re.compile(r"debian-(\d+)-amd64-")


NESTED_VIRTUALIZATION_FEATURE = "nested-virtualization"
BYTES_IN_GIB = 1_073_741_824
ARCHITECTURES: Mapping[str, str] = {"x86_64": "amd64"}


class AwsCatalog:
	"""Translate AWS catalog records into Atlas catalog values."""

	def get_server_sizes(self, instance_types: object) -> tuple[ServerSizeData, ...]:
		"""Return Atlas sizes for the AWS instance types that can run virtual machines."""
		if not isinstance(instance_types, list) or not all(
			isinstance(item, Mapping) for item in instance_types
		):
			raise AwsError("AWS response has invalid instance types")

		return tuple(
			self._server_size(instance_type)
			for instance_type in instance_types
			if self.is_supported(instance_type)
		)

	def get_server_images(self, images: object) -> tuple[ServerImageData, ...]:
		"""Return Atlas images for the newest supported AWS machine image of each version."""
		if not isinstance(images, list) or not all(isinstance(image, Mapping) for image in images):
			raise AwsError("AWS response has invalid machine images")

		newest: dict[str, tuple[str, ServerImageData]] = {}
		for image in images:
			accepted = self._accepted_image(image)
			if accepted is None:
				continue
			creation_date = str(image.get("CreationDate") or "")
			current = newest.get(accepted.name)
			if current is None or creation_date > current[0]:
				newest[accepted.name] = (creation_date, accepted)
		return tuple(image for _, image in newest.values())

	@staticmethod
	def is_supported(instance_type: Mapping) -> bool:
		"""Report whether a type supports Atlas without local storage."""
		processor = instance_type.get("ProcessorInfo")
		features = processor.get("SupportedFeatures", []) if isinstance(processor, Mapping) else []

		has_virtualization = bool(instance_type.get("BareMetal")) or (
			NESTED_VIRTUALIZATION_FEATURE in features
		)
		return has_virtualization and AwsCatalog._disk_gib(instance_type) == 0

	@staticmethod
	def image_id(metadata: object, image_name: str) -> str:
		"""Return the AWS machine image ID from provider image metadata."""
		image_id = metadata.get("ImageId") if isinstance(metadata, Mapping) else None
		if not isinstance(image_id, str):
			raise AwsError(f"Metal Server Image {image_name} has no AWS machine image ID")
		return image_id

	@staticmethod
	def root_device_name(metadata: object, image_name: str) -> str:
		"""Return the AWS root device name from provider image metadata."""
		device_name = metadata.get("RootDeviceName") if isinstance(metadata, Mapping) else None
		if not isinstance(device_name, str):
			raise AwsError(f"Metal Server Image {image_name} has no AWS root device name")
		return device_name

	def _server_size(self, instance_type: Mapping) -> ServerSizeData:
		name = instance_type.get("InstanceType")
		if not isinstance(name, str):
			raise AwsError("AWS instance type has no name")
		processor = instance_type.get("VCpuInfo")
		memory = instance_type.get("MemoryInfo")
		cpu_count = processor.get("DefaultVCpus") if isinstance(processor, Mapping) else None
		memory_mib = memory.get("SizeInMiB") if isinstance(memory, Mapping) else None
		if not isinstance(cpu_count, int) or not isinstance(memory_mib, int):
			raise AwsError(f"AWS instance type {name} has invalid CPU or memory data")

		return ServerSizeData(
			name=name,
			architecture=self.instance_type_architecture(instance_type),
			cpu_count=cpu_count,
			memory_mib=memory_mib,
			disk_gib=STORAGE_VOLUME_SIZE_GIB,
			hourly_pricing_usd_cents=None,
			monthly_pricing_usd_cents=None,
			provider_metadata=dict(instance_type),
		)

	@staticmethod
	def instance_type_architecture(instance_type: Mapping) -> str:
		"""Return the Atlas architecture reported by one AWS instance type.

		Atlas fetches x86_64 instance types only. AWS lists every architecture a type
		can boot, so a 64-bit type also reports i386.
		"""
		name = instance_type.get("InstanceType")
		processor = instance_type.get("ProcessorInfo")
		supported = processor.get("SupportedArchitectures") if isinstance(processor, Mapping) else None
		if not isinstance(supported, list) or not supported:
			raise AwsError(f"AWS instance type {name} has no supported architectures")

		architectures = {ARCHITECTURES[value] for value in supported if value in ARCHITECTURES}
		if len(architectures) != 1:
			raise AwsError(f"AWS instance type {name} has no single Atlas architecture")
		return architectures.pop()

	@staticmethod
	def _disk_gib(instance_type: Mapping) -> int:
		"""Return the instance store size, which Atlas uses for the storage pool.

		AWS reports decimal gigabytes and Atlas stores binary gibibytes.
		"""
		storage = instance_type.get("InstanceStorageInfo")
		disk_gb = storage.get("TotalSizeInGB", 0) if isinstance(storage, Mapping) else 0
		if not isinstance(disk_gb, int):
			return 0

		return disk_gb * 1_000_000_000 // BYTES_IN_GIB

	@staticmethod
	def _accepted_image(image: Mapping) -> ServerImageData | None:
		name = image.get("Name")
		if not isinstance(name, str):
			return None

		ubuntu = UBUNTU_IMAGE_NAME.match(name)
		debian = DEBIAN_IMAGE_NAME.match(name)
		if ubuntu:
			os_name, version = "Ubuntu", ubuntu.group(1)
		elif debian:
			os_name, version = "Debian", debian.group(1)
		else:
			return None

		if version not in ACCEPTED_OS_VERSIONS.get(os_name, ()):
			return None

		return ServerImageData(
			name=f"{os_name}_{version}",
			os=os_name,
			version=version,
			provider_metadata=dict(image),
		)
