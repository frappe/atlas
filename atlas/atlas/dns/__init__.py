"""DNS provider registry — twin of `atlas/atlas/providers/__init__.py`.

Vendors register their `DnsProvider` subclass via `@register`. Callers ask for an
instance via `for_dns_provider_type(provider_type)`, which maps the type to its
registered implementation class. There is no `Domain Provider` DocType row to
load: the active DNS vendor is `Atlas Settings.dns_provider_type`, and a
`Root Domain` carries its own denormalized `dns_provider_type`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frappe

# Re-exported for callers (the TLS Certificate controller builds `dns.WildcardTargets`).
# base.py is stdlib-only at import time, so this triggers no boto3 load.
from atlas.atlas.dns.base import WildcardTargets

if TYPE_CHECKING:
	from atlas.atlas.dns.base import DnsProvider

__all__ = ["WildcardTargets", "for_dns_provider_type", "register"]


_REGISTRY: dict[str, type["DnsProvider"]] = {}


def register(cls: type["DnsProvider"]) -> type["DnsProvider"]:
	"""Class decorator that records `cls` against its `provider_type`."""
	_REGISTRY[cls.provider_type] = cls
	return cls


def for_dns_provider_type(provider_type: str) -> "DnsProvider":
	"""Return an instantiated `DnsProvider` for the given `provider_type`.

	Raises `frappe.ValidationError` if the type has no registered implementation.
	"""
	_load_implementations()
	factory = _REGISTRY.get(provider_type)
	if factory is None:
		frappe.throw(f"No implementation for provider_type {provider_type!r}")
	return factory()


def _load_implementations() -> None:
	"""Import vendor modules so their `@register` decorators run. Idempotent —
	Python caches the import. Separate so tests that stub the registry can skip it."""
	import atlas.atlas.dns.powerdns
	import atlas.atlas.dns.route53
