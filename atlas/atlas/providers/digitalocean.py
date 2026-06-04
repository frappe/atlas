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
)
from atlas.atlas.networking import carve_virtual_machine_range
from atlas.atlas.providers import register
from atlas.atlas.providers.base import (
	AuthResult,
	Capabilities,
	ImageInfo,
	Provider,
	ProvisionRequest,
	ProvisionResult,
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


def _strip_prefix(value: str, provider_type: str) -> str:
	prefix = f"{provider_type}/"
	if value and value.startswith(prefix):
		return value[len(prefix):]
	return value
