"""Fake provider — a no-op vendor for development.

Lets an engineer exercise the whole Server / Virtual Machine lifecycle without a
real cloud account or a real host: `provision()` returns a host that is already
`ready` with synthetic networking, `destroy()` releases nothing, and the
companion task seam (`fake_tasks.run_fake_task`) makes every Task on a Fake
server succeed (or fail on demand) without SSH.

Two safeties:

- **`developer_mode` gate.** Every *mutating* method throws unless
  `frappe.conf.developer_mode` is on, so a Fake `Provider` row on a production
  site is inert and loud rather than silently active.
- **Unroutable addresses.** Synthetic IPs come from the documentation/test
  ranges (IPv4 TEST-NET-3 `203.0.113.0/24`, RFC 5737; IPv6 `2001:db8::/32`,
  RFC 3849), so even an accidental real `ssh` to a Fake host can never reach a
  stranger's machine.

The Frappe DB is the source of truth (spec operating principle #2), so the fake
holds no state of its own — networking is derived deterministically from the
server title, which makes re-running the demo script idempotent and keeps two
servers from colliding.
"""

from __future__ import annotations

import hashlib

import frappe

from atlas.atlas.providers import register
from atlas.atlas.providers.base import (
	AuthResult,
	Capabilities,
	ImageInfo,
	Provider,
	ProvisionRequest,
	ProvisionResult,
	ReservedIp,
	ServerNetworking,
	SizeInfo,
)

FAKE_PROVIDER_TYPE = "Fake"

# A small synthetic catalog so the Provision dialog and capacity math have data.
FAKE_SIZES: tuple[SizeInfo, ...] = (
	SizeInfo(slug="fake-1vcpu-1gb", monthly_cost_usd=6, provider_metadata={"vcpus": 1, "memory_mb": 1024}),
	SizeInfo(slug="fake-2vcpu-4gb", monthly_cost_usd=24, provider_metadata={"vcpus": 2, "memory_mb": 4096}),
	SizeInfo(slug="fake-4vcpu-8gb", monthly_cost_usd=48, provider_metadata={"vcpus": 4, "memory_mb": 8192}),
	SizeInfo(slug="fake-8vcpu-16gb", monthly_cost_usd=96, provider_metadata={"vcpus": 8, "memory_mb": 16384}),
)
FAKE_IMAGES: tuple[ImageInfo, ...] = (
	ImageInfo(slug="ubuntu-24.04", provider_metadata={"distribution": "Ubuntu", "release": "24.04"}),
	ImageInfo(slug="ubuntu-24.04-minimal", provider_metadata={"distribution": "Ubuntu", "release": "24.04"}),
	ImageInfo(slug="debian-12", provider_metadata={"distribution": "Debian", "release": "12"}),
)

DEFAULT_FAKE_SIZE = f"{FAKE_PROVIDER_TYPE}/fake-2vcpu-4gb"
DEFAULT_FAKE_IMAGE = f"{FAKE_PROVIDER_TYPE}/ubuntu-24.04"


def require_developer_mode() -> None:
	"""Throw unless the site is in developer_mode. The gate that keeps a Fake
	provider inert on a production site."""
	if not frappe.conf.developer_mode:
		frappe.throw("The Fake provider is only available when developer_mode is enabled")


@register
class FakeProvider(Provider):
	provider_type = FAKE_PROVIDER_TYPE
	# describe() is ready at once; keep the worker's poll loop trivially short.
	ready_timeout_seconds = 10

	def authenticate(self) -> AuthResult:
		require_developer_mode()
		return AuthResult(ok=True, account_label="fake")

	def discover(self) -> Capabilities:
		return Capabilities(sizes=FAKE_SIZES, images=FAKE_IMAGES)

	def provision(self, request: ProvisionRequest) -> ProvisionResult:
		require_developer_mode()
		return ProvisionResult(
			provider_resource_id=f"fake-{_token(request.title)}",
			size=request.size or DEFAULT_FAKE_SIZE,
			image=request.image or DEFAULT_FAKE_IMAGE,
			ready=True,
			# Key networking off the resource id (the same token describe() sees),
			# so provision() and describe() agree without describe() needing the title.
			networking=_fake_networking(_token(request.title)),
			provider_metadata={"fake": True, "title": request.title},
		)

	def describe(self, provider_resource_id: str) -> ProvisionResult:
		# Already ready at provision; the worker still polls once. Networking is
		# re-derived from the id's token so the result matches provision()'s.
		token = provider_resource_id.removeprefix("fake-")
		return ProvisionResult(
			provider_resource_id=provider_resource_id,
			size=DEFAULT_FAKE_SIZE,
			image=DEFAULT_FAKE_IMAGE,
			ready=True,
			networking=_fake_networking(token),
			provider_metadata={"fake": True},
		)

	def destroy(self, provider_resource_id: str) -> None:
		# Nothing was ever allocated at a vendor.
		return None

	# prepare_host: inherit the no-op default (a Fake host exposes root directly).

	# --- Reserved IPs ----------------------------------------------------
	# allocate() hands out a deterministic unroutable v4; the Frappe `Reserved
	# IP` row is the only state, so assign/unassign/release are no-ops and
	# list/discover have nothing to reconcile.

	def allocate_reserved_ip(self) -> ReservedIp:
		require_developer_mode()
		address = _fake_public_ipv4()
		return ReservedIp(ip_address=address, provider_resource_id=f"fake-rip-{address}")

	def assign_reserved_ip(self, provider_resource_id: str, droplet_resource_id: str) -> None:
		return None

	def unassign_reserved_ip(self, provider_resource_id: str) -> None:
		return None

	def list_reserved_ips(self) -> tuple[ReservedIp, ...]:
		return ()

	def release_reserved_ip(self, provider_resource_id: str) -> None:
		return None


def _token(seed: str) -> str:
	"""A short, stable hex token derived from `seed` — used to make synthetic
	identifiers and addresses deterministic per server title."""
	return hashlib.sha256(seed.encode()).hexdigest()[:8]


def _byte(seed: str, salt: str) -> int:
	"""A stable value in 1..254 derived from `seed`+`salt`, for a host octet
	or hextet that is never 0 or 255."""
	digest = hashlib.sha256(f"{seed}/{salt}".encode()).digest()
	return digest[0] % 254 + 1


def _fake_networking(title: str) -> ServerNetworking:
	"""Synthetic, deterministic, unroutable networking for a Fake server.

	IPv4 in TEST-NET-3 (203.0.113.0/24, RFC 5737); IPv6 host + a /124 VM range
	under 2001:db8::/32 (RFC 3849). The host part is derived from the title so
	re-provisioning the same demo server is stable and two servers differ."""
	host_octet = _byte(title, "v4")
	v6_group = format(_byte(title, "v6") * 256 + _byte(title, "v6b"), "x")
	return ServerNetworking(
		ipv4_address=f"203.0.113.{host_octet}",
		ipv6_address=f"2001:db8:{v6_group}::1",
		ipv6_prefix=f"2001:db8:{v6_group}::/64",
		ipv6_virtual_machine_range=f"2001:db8:{v6_group}::/124",
	)


def _fake_public_ipv4() -> str:
	"""A fresh unroutable public v4 for a Reserved IP. Drawn from TEST-NET-2
	(198.51.100.0/24) — a different documentation block than the servers'
	TEST-NET-3, so a reserved IP never visually collides with a host's SSH
	endpoint. The row's `unique` `ip_address` is the real guard against dupes."""
	suffix = frappe.generate_hash("fake-rip", 4)
	return f"198.51.100.{int(suffix, 16) % 254 + 1}"
