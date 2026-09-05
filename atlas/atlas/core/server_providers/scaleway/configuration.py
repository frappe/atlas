from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


@dataclass(frozen=True, slots=True)
class ScalewayConfiguration:
	"""Store the Scaleway settings that low-level operations use."""

	project_id: str
	organization_id: str | None
	zone: str
	region: str
	resource_name_prefix: str
	private_network_id: str | None
	ssh_key_id: str | None
	billing_cycle: str

	@classmethod
	def from_settings(cls, settings: "AtlasSettings") -> "ScalewayConfiguration":
		"""Create a Scaleway configuration from Atlas Settings."""
		return cls(
			project_id=settings.scaleway_project_id,
			organization_id=settings.scaleway_organization_id,
			zone=settings.scaleway_zone,
			region=settings.scaleway_zone.rsplit("-", 1)[0],
			resource_name_prefix=settings.resource_name_prefix,
			private_network_id=settings.scaleway_private_network_id,
			ssh_key_id=settings.scaleway_ssh_key_id,
			billing_cycle=settings.scaleway_machine_billing_cycle,
		)
