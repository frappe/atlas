"""DigitalOcean provider implementation.

Reads `DigitalOcean Settings` for the API token / region / defaults.
Reads `Atlas Settings` (indirectly via `atlas.get_ssh_key()`) for the
SSH key. Delegates HTTP to `atlas.atlas.digitalocean.DigitalOceanClient`.

`discover()` intentionally does not query the DO catalog API — the
catalog endpoint is paginated, the slugs we trust have stable names, and
we don't want first-load latency in the desk. The hand-maintained
constants below are the source of truth. A `api=True` toggle that hits
the real endpoint is a future seam.
"""

from __future__ import annotations

import frappe

from atlas.atlas.digitalocean import (
	DigitalOceanClient,
	DigitalOceanError,
	public_ipv4,
	public_ipv6,
	reserved_ip_droplet_id,
)
from atlas.atlas.networking import carve_virtual_machine_range
from atlas.atlas.providers import register
from atlas.atlas.providers.base import (
	AuthResult,
	Capabilities,
	DiscoveredServer,
	ImageInfo,
	Provider,
	ProvisionRequest,
	ProvisionResult,
	ReservedIp,
	ServerNetworking,
	SizeInfo,
)
from atlas.atlas.secrets import get_secret

# Monthly USD price per size. Hand-maintained — DO does not expose a
# stable per-size cost endpoint. Renders as "—" when blank.
DIGITALOCEAN_MONTHLY_COST_USD: dict[str, int] = {
	"s-1vcpu-1gb": 6,
	"s-1vcpu-2gb": 12,
	"s-2vcpu-2gb": 18,
	"s-2vcpu-4gb-intel": 24,
	"s-2vcpu-4gb": 24,
	"s-4vcpu-8gb": 48,
	"s-8vcpu-16gb-intel": 96,
	"s-8vcpu-16gb": 96,
	"c-2": 40,
	"c-4": 80,
}

KNOWN_DIGITALOCEAN_SIZES: tuple[str, ...] = tuple(DIGITALOCEAN_MONTHLY_COST_USD.keys())

KNOWN_DIGITALOCEAN_IMAGES: tuple[str, ...] = (
	"ubuntu-24-04-x64",
	"ubuntu-22-04-x64",
)


@register
class DigitalOceanProvider(Provider):
	provider_type = "DigitalOcean"

	def __init__(self) -> None:
		settings = frappe.get_single("DigitalOcean Settings")
		token = get_secret("DigitalOcean Settings", "DigitalOcean Settings", "api_token")
		self.client = DigitalOceanClient(token=token)
		self.region = settings.region
		self.default_size = settings.default_size
		self.default_image = settings.default_image

	def authenticate(self) -> AuthResult:
		try:
			result = self.client.verify_credentials()
		except DigitalOceanError as exception:
			return AuthResult(ok=False, error=str(exception))
		return AuthResult(
			ok=True,
			account_label=result.get("email"),
			rate_limit=result.get("rate_limit"),
			rate_remaining=result.get("rate_remaining"),
		)

	def discover(self) -> Capabilities:
		sizes = tuple(
			SizeInfo(slug=slug, monthly_cost_usd=DIGITALOCEAN_MONTHLY_COST_USD.get(slug))
			for slug in KNOWN_DIGITALOCEAN_SIZES
		)
		images = tuple(ImageInfo(slug=slug) for slug in KNOWN_DIGITALOCEAN_IMAGES)
		return Capabilities(sizes=sizes, images=images)

	def provision(self, request: ProvisionRequest) -> ProvisionResult:
		size_slug = _strip_prefix(request.size, self.provider_type)
		image_slug = _strip_prefix(request.image, self.provider_type)
		ssh_key_ids = []
		if request.ssh_key and request.ssh_key.vendor_id:
			ssh_key_ids.append(request.ssh_key.vendor_id)
		droplet = self.client.create_droplet(
			name=request.title,
			region=self.region,
			size=size_slug,
			image=image_slug,
			ssh_key_ids=ssh_key_ids,
			tags=list(request.tags),
			ipv6=True,
		)
		return ProvisionResult(
			provider_resource_id=str(droplet["id"]),
			size=request.size,
			image=request.image,
			ready=False,
			networking=None,
			provider_metadata=droplet,
		)

	def describe(self, provider_resource_id: str) -> ProvisionResult:
		droplet = self.client.get_droplet(int(provider_resource_id))
		size_name = f"{self.provider_type}/{droplet.get('size_slug')}" if droplet.get("size_slug") else ""
		image_slug = (droplet.get("image") or {}).get("slug")
		image_name = f"{self.provider_type}/{image_slug}" if image_slug else ""
		if droplet.get("status") != "active":
			return ProvisionResult(
				provider_resource_id=provider_resource_id,
				size=size_name,
				image=image_name,
				ready=False,
				networking=None,
				provider_metadata=droplet,
			)
		ipv4 = public_ipv4(droplet)
		ipv6_address, ipv6_prefix = public_ipv6(droplet)
		vm_range = carve_virtual_machine_range(ipv6_address, ipv6_prefix)
		networking = ServerNetworking(
			ipv4_address=ipv4,
			ipv6_address=ipv6_address,
			ipv6_prefix=ipv6_prefix,
			ipv6_virtual_machine_range=vm_range,
		)
		return ProvisionResult(
			provider_resource_id=provider_resource_id,
			size=size_name,
			image=image_name,
			ready=True,
			networking=networking,
			provider_metadata=droplet,
		)

	def destroy(self, provider_resource_id: str) -> None:
		self.client.delete_droplet(int(provider_resource_id))

	def list_servers(self) -> tuple[DiscoveredServer, ...]:
		"""Every droplet in the account, for discover/import. The size label
		mirrors describe()'s `DigitalOcean/<size_slug>` form so the preview reads
		like the catalog. IPv4 is best-effort (a new droplet may have none yet —
		describe() is the authority at import)."""
		return tuple(
			_discovered_from_droplet(self.provider_type, droplet) for droplet in self.client.list_droplets()
		)

	# --- Reserved IPs ----------------------------------------------------
	# On DigitalOcean a reserved IP is keyed by its own address, so the
	# vendor handle (`provider_resource_id`) IS the IP string. The droplet
	# handle is the droplet id as a string (matching `Server.provider_resource_id`).

	def allocate_reserved_ip(self) -> ReservedIp:
		reserved = self.client.create_reserved_ip(self.region)
		return _reserved_ip_from_payload(reserved)

	def assign_reserved_ip(self, provider_resource_id: str, droplet_resource_id: str) -> None:
		self.client.assign_reserved_ip(provider_resource_id, int(droplet_resource_id))

	def unassign_reserved_ip(self, provider_resource_id: str) -> None:
		self.client.unassign_reserved_ip(provider_resource_id)

	def list_reserved_ips(self) -> tuple[ReservedIp, ...]:
		return tuple(_reserved_ip_from_payload(r) for r in self.client.list_reserved_ips())

	def release_reserved_ip(self, provider_resource_id: str) -> None:
		self.client.delete_reserved_ip(provider_resource_id)


def _discovered_from_droplet(provider_type: str, droplet: dict) -> DiscoveredServer:
	"""Map a raw DO droplet payload to a DiscoveredServer for the picker. The
	size label mirrors describe()'s `<provider_type>/<size_slug>` form; the IPv4
	is best-effort (`public_ipv4` raises on a droplet with no public v4 yet, so a
	new/locked droplet must not break discovery)."""
	size_slug = droplet.get("size_slug")
	size = f"{provider_type}/{size_slug}" if size_slug else None
	try:
		ipv4 = public_ipv4(droplet)
	except DigitalOceanError:
		ipv4 = None
	return DiscoveredServer(
		provider_resource_id=str(droplet["id"]),
		title=droplet.get("name") or None,
		ipv4_address=ipv4,
		size=size,
		provider_metadata=droplet,
	)


def _reserved_ip_from_payload(reserved: dict) -> ReservedIp:
	ip = reserved["ip"]
	droplet_id = reserved_ip_droplet_id(reserved)
	return ReservedIp(
		ip_address=ip,
		provider_resource_id=ip,
		droplet_resource_id=str(droplet_id) if droplet_id is not None else None,
		provider_metadata=reserved,
	)


def _strip_prefix(value: str, provider_type: str) -> str:
	prefix = f"{provider_type}/"
	if value and value.startswith(prefix):
		return value[len(prefix) :]
	return value
