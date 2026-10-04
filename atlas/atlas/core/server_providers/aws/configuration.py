from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

ROOT_VOLUME_SIZE_GIB = 64
STORAGE_VOLUME_SIZE_GIB = 500
STORAGE_VOLUME_DEVICE_NAME = "/dev/sdb"


@dataclass(frozen=True, slots=True)
class AwsConfiguration:
	"""Store the AWS settings that low-level operations use."""

	region: str
	availability_zone: str
	region_name: str
	vpc_id: str | None
	subnet_id: str | None
	security_group_id: str | None
	key_pair_name: str | None

	@classmethod
	def from_settings(cls, settings: "AtlasSettings") -> "AwsConfiguration":
		"""Create an AWS configuration from Atlas Settings."""
		return cls(
			region=settings.aws_region,
			availability_zone=settings.aws_availability_zone,
			region_name=settings.region_name,
			vpc_id=settings.aws_vpc_id,
			subnet_id=settings.aws_subnet_id,
			security_group_id=settings.aws_security_group_id,
			key_pair_name=settings.aws_key_pair_name,
		)

	def resource_name(self, detail: str) -> str:
		"""Return the display name of one Atlas resource in AWS."""
		return f"Atlas - {self.region_name} - {detail}"
