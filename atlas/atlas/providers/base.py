"""Provider abstraction.

Five methods: authenticate / discover / provision / describe / destroy.
Atlas talks to vendors only through this interface; the indirection through
`atlas.get_provider()` means callers never branch on `provider_type`.
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
	# Vendor's handle for the key (DO fingerprint, AWS KeyPair name).
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


@dataclasses.dataclass(frozen=True, slots=True)
class ImageInfo:
	slug: str
	provider_metadata: dict | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Capabilities:
	sizes: tuple[SizeInfo, ...]
	images: tuple[ImageInfo, ...]
	quota: dict | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class AuthResult:
	ok: bool
	account_label: str | None = None
	rate_limit: int | None = None
	rate_remaining: int | None = None
	missing_scopes: tuple[str, ...] = ()
	error: str | None = None


class Provider(ABC):
	provider_type: ClassVar[str]

	@abstractmethod
	def authenticate(self) -> AuthResult:
		...

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
