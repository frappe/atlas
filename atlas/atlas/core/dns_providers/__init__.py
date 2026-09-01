from __future__ import annotations

from typing import TYPE_CHECKING

import frappe

from atlas.atlas.core.dns_providers.base import DnsProvider

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

_REGISTRY: dict[str, type[DnsProvider]] = {}


def register(provider_class: type[DnsProvider]) -> type[DnsProvider]:
	"""Use as a decorator on each DNS provider implementation."""
	_REGISTRY[provider_class.provider_type] = provider_class
	return provider_class


def get_dns_provider(
	provider_type: str | None = None, settings: "AtlasSettings | None" = None
) -> DnsProvider:
	"""Return the selected DNS provider implementation. Pass `settings` to use its current values."""
	_load_implementations()

	resolved_type = provider_type or (
		settings.dns_provider if settings else frappe.get_single_value("Atlas Settings", "dns_provider")
	)
	provider_class = _REGISTRY.get(resolved_type)
	if provider_class is None:
		frappe.throw(f"No implementation for DNS provider type {resolved_type!r}")

	return provider_class(settings)


def _load_implementations() -> None:
	import atlas.atlas.core.dns_providers.route53


__all__ = ["DnsProvider", "get_dns_provider", "register"]
