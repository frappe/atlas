from __future__ import annotations

from atlas.api.core.base import Router
from atlas.api.core.docs import DocsConfig

API_PREFIX = "atlas"
API_VERSION = "1.0.0"

atlas_router = Router(
	prefix=API_PREFIX,
	name="Atlas",
	description="Tenant control plane for virtual machines, IP addresses, and images.",
	docs=DocsConfig(title="Atlas API", version=API_VERSION),
)

virtual_machine_group = atlas_router.subrouter(
	"virtual-machines",
	name="Virtual Machines",
	description="Create, manage, and terminate tenant virtual machines.",
	tag_kind="nav",
)

virtual_machines = virtual_machine_group.subrouter(
	"",
	name="VM Lifecycle",
	description="Create, read, and terminate virtual machines.",
	tag_parent="Virtual Machines",
)

virtual_machine_actions = virtual_machine_group.subrouter(
	"",
	name="VM Actions",
	description="Control virtual machine state and create snapshots and console tokens.",
	tag_parent="Virtual Machines",
)

virtual_machine_configuration = virtual_machine_group.subrouter(
	"",
	name="VM Configuration",
	description="Change virtual machine compute, storage, network, and guest settings.",
	tag_parent="Virtual Machines",
)

ip_addresses = atlas_router.subrouter(
	"ip-addresses",
	name="IP Addresses",
	description="Reserve and release public IPv4 addresses for one tenant.",
)

images = atlas_router.subrouter(
	"images",
	name="Images",
	description="Read and delete tenant virtual machine images.",
)


def get_resource_location(collection: str, resource_id: str) -> str:
	"""Return the Location header value for one created resource."""
	return f"{atlas_router.prefix}/{collection}/{resource_id}"


def register_atlas_api() -> None:
	"""Import each module that registers Atlas API routes."""
	import atlas.api.routes.images
	import atlas.api.routes.ip_addresses
	import atlas.api.routes.jwks
	import atlas.api.routes.virtual_machines
