from __future__ import annotations

from typing import TYPE_CHECKING

import frappe

from atlas.atlas.core.metal_providers.base import MetalProvider

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

_REGISTRY: dict[str, type[MetalProvider]] = {}


def register(provider_class: type[MetalProvider]) -> type[MetalProvider]:
	"""Use as a decorator on each metal provider implementation."""
	_REGISTRY[provider_class.provider_type] = provider_class
	return provider_class


def get_metal_provider(
	provider_type: str | None = None, settings: "AtlasSettings | None" = None
) -> MetalProvider:
	"""Return the selected metal provider implementation. Pass `settings` to use its current values."""
	_load_implementations()

	resolved_type = provider_type or (
		settings.metal_provider if settings else frappe.get_single_value("Atlas Settings", "metal_provider")
	)
	provider_class = _REGISTRY.get(resolved_type)
	if provider_class is None:
		frappe.throw(f"No implementation for metal provider type {resolved_type!r}")

	return provider_class(settings)


def _load_implementations() -> None:
	import atlas.atlas.core.metal_providers.scaleway


__all__ = ["MetalProvider", "get_metal_provider", "register"]
