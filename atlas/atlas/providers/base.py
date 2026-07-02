"""Provider abstraction.

Server lifecycle: authenticate / discover / provision / describe / destroy.
Reserved-IP lifecycle (the inbound-v4 primitive): allocate_reserved_ip /
assign_reserved_ip / unassign_reserved_ip / list_reserved_ips /
release_reserved_ip. Atlas talks to vendors only through this interface; the
indirection through `atlas.get_provider()` means callers never branch on
`provider_type`.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from enum import Enum
from typing import ClassVar


class Networking(Enum):
	IPV4_ONLY = "ipv4"
	IPV6_ONLY = "ipv6"
	DUAL_STACK = "dual"


@dataclasses.dataclass(frozen=True, slots=True)
class SshKey:
	# Vendor's handle for the key — whatever the provider's create-host call
	# expects to reference a pre-registered key (DigitalOcean: the key's id or
	# fingerprint; AWS: the KeyPair name).
	vendor_id: str | None = None
	# Body, for vendors that upload at provision-time.
	public_key: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ServerNetworking:
	ipv4_address: str | None
	ipv6_address: str | None
	ipv6_prefix: str | None
	ipv6_virtual_machine_range: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class ProvisionRequest:
	title: str
	size: str
	image: str
	ssh_key: SshKey
	networking: Networking = Networking.DUAL_STACK
	tags: tuple[str, ...] = ()
	cloud_init: str | None = None
	# Self-Managed only: operator-supplied networking comes through here.
	prebuilt_networking: ServerNetworking | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ProvisionResult:
	"""Returned by provision() and describe(). Often a partial."""

	provider_resource_id: str
	size: str
	image: str
	ready: bool
	networking: ServerNetworking | None = None
	provider_metadata: dict | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class SizeInfo:
	slug: str
	monthly_cost_usd: int | None
	provider_metadata: dict | None = None
	# The provider's opinionated default for the Provision Server modal. Only one
	# size per discover() should set it. `upsert_catalog` honours the hint solely
	# when no row of this provider_type is already marked default — an explicit
	# operator/config choice (and a later manual flip) always wins.
	is_default: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class ImageInfo:
	slug: str
	provider_metadata: dict | None = None
	is_default: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class Capabilities:
	sizes: tuple[SizeInfo, ...]
	images: tuple[ImageInfo, ...]
	quota: dict | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ReservedIp:
	"""A public IPv4 reserved at the vendor, optionally bound to a host.

	`provider_resource_id` is the vendor's handle (on DigitalOcean the reserved
	IP is keyed by its address, so the two are equal there; other vendors may
	differ). `droplet_resource_id` is the provider_resource_id of the *Server*
	(droplet) the IP is currently assigned to, or None if it's floating —
	enough for `discover()` to map a vendor reserved IP back to a Server row."""

	ip_address: str
	provider_resource_id: str
	droplet_resource_id: str | None = None
	provider_metadata: dict | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class DiscoveredServer:
	"""A server the vendor account already holds, for discover/import. Maps back
	to a Server row via `provider_resource_id` (the dedup key). Carries just
	enough to render the import picker — id, vendor hostname, an IPv4 and a size
	label for the preview row. Import does NOT trust these into immutable Server
	fields; it re-resolves each picked id authoritatively via `describe()`."""

	provider_resource_id: str
	title: str | None = None  # vendor hostname; import falls back to the id
	ipv4_address: str | None = None  # for the preview row only
	size: str | None = None  # vendor offer/size label, for the preview row
	provider_metadata: dict | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class AuthResult:
	ok: bool
	account_label: str | None = None
	rate_limit: int | None = None
	rate_remaining: int | None = None
	missing_scopes: tuple[str, ...] = ()
	error: str | None = None


class ProviderError(Exception):
	"""A terminal, non-retryable provider failure raised from `describe()`.

	The worker's poll loop lets this propagate (rather than spinning until the
	ready timeout) so the Server is marked `Broken` immediately. Used for vendor
	states a poll can never recover from — a Scaleway server stuck in
	`error`/`out_of_stock`/`locked`, a deleted droplet, etc."""


class Provider(ABC):
	provider_type: ClassVar[str]

	# How long the worker polls `describe()` for `ready=True` before giving up.
	# Droplet-style vendors are ready in seconds; bare-metal installs (Scaleway)
	# take minutes, so they override this. The worker reads it off the instance.
	ready_timeout_seconds: ClassVar[int] = 600

	@abstractmethod
	def authenticate(self) -> AuthResult: ...

	@abstractmethod
	def discover(self) -> Capabilities:
		"""Return the vendor's current catalog. Callers upsert `Provider Size`
		/ `Provider Image` rows; slugs missing from the result get disabled."""
		...

	@abstractmethod
	def provision(self, request: ProvisionRequest) -> ProvisionResult:
		"""Allocate the vendor resource. Must return within 30s. `ready=False`
		is fine; `describe()` fills the rest."""
		...

	@abstractmethod
	def describe(self, provider_resource_id: str) -> ProvisionResult:
		"""Read-only, pollable. Authoritative source for Server fields after
		provision. `ready=True` means networking is fully populated."""
		...

	@abstractmethod
	def destroy(self, provider_resource_id: str) -> None:
		"""Release the vendor resource. Idempotent. Called from
		`Server.archive()`."""
		...

	@abstractmethod
	def list_servers(self) -> tuple[DiscoveredServer, ...]:
		"""Every server the account holds in this region/zone — UNFILTERED (not
		tag-scoped), so a host built outside Atlas is discoverable. For the
		discover/import picker: the preview renders these; import re-resolves each
		picked id authoritatively via `describe()`. Vendors without an API
		(Self-Managed) return `()`."""
		...

	def prepare_host(self, server) -> None:
		"""Hook run by the worker after the host is `ready` but BEFORE Atlas's
		root-SSH wait + bootstrap. Default: no-op (DO/Self-Managed expose root
		directly).

		Scaleway's Ubuntu image force-blocks root login (the cloud-image
		forced-command on root's authorized_keys); this hook does the one-shot
		'first contact' as the `ubuntu` user — copy authorized_keys to /root and
		strip the forced-command — so the rest of Atlas keeps using root
		unchanged. Idempotent: re-running on an already-root-enabled host is a
		harmless overwrite."""
		return None

	def vm_range_is_forwardable(self, provider_resource_id: str) -> bool:
		"""True iff a VM keeping its /128 across a migration can have its inbound
		traffic permanently forwarded from the source host to wherever it now lives
		(spec/24-vm-migration.md §2.8). This is the keep-address gate: the source
		host keeps holding the /64 the /128 is carved from — so it keeps receiving
		the address and tunnels it to the target — and nothing ever reclaims or
		moves the /64. Default False (Self-Managed, Fake) → those providers fall
		back to change-address (a NEW /128 on the target + a proxy re-point).

		Unlike moving the whole /64, this needs no provider API call: it is a fact
		about the delivery mechanism (routed-prefix or proxy-NDP), not a movable
		vendor resource. `provider_resource_id` is accepted for symmetry with the
		other capability methods and in case a future provider must probe per-box;
		the current overrides ignore it."""
		return False

	# --- Reserved IPs (the inbound-v4 primitive) -------------------------
	# A reserved IP is allocated to a region, assigned to the *droplet*
	# (host), and host-side 1:1-NATed to the guest by a later Task. The
	# provider owns only the vendor object; the Frappe invariant (one IP, one
	# VM, same Server) lives in `Reserved IP.attach()`/`detach()`.

	@abstractmethod
	def allocate_reserved_ip(self) -> ReservedIp:
		"""Reserve a new public IPv4 in the provider's region, unassigned. The
		caller writes a `Reserved IP` row from the result and assigns it on
		attach. Atlas is single-region, so the provider sources the region."""
		...

	@abstractmethod
	def assign_reserved_ip(self, provider_resource_id: str, droplet_resource_id: str) -> None:
		"""Bind the reserved IP to a Server (droplet). Idempotent — a no-op if
		already assigned there."""
		...

	@abstractmethod
	def unassign_reserved_ip(self, provider_resource_id: str) -> None:
		"""Release the reserved IP from its droplet, leaving it allocated to the
		region. Idempotent."""
		...

	@abstractmethod
	def list_reserved_ips(self) -> tuple[ReservedIp, ...]:
		"""Every reserved IP the account holds, for discover/import. Maps a
		vendor reserved IP back to a Server via `droplet_resource_id`."""
		...

	@abstractmethod
	def release_reserved_ip(self, provider_resource_id: str) -> None:
		"""Destroy the reserved IP at the vendor. Idempotent. Called when a
		`Reserved IP` row is deleted from the pool."""
		...
