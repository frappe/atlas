from __future__ import annotations

from typing import TYPE_CHECKING

import frappe

from atlas.atlas.core.server_providers.base import ServerProvider

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

_REGISTRY: dict[str, type[ServerProvider]] = {}


def register(provider_class: type[ServerProvider]) -> type[ServerProvider]:
	"""Use as a decorator on each server provider implementation."""
	_REGISTRY[provider_class.provider_type] = provider_class
	return provider_class


def get_server_provider(
	provider_type: str | None = None, settings: "AtlasSettings | None" = None
) -> ServerProvider:
	"""Return the selected server provider implementation. Pass `settings` to use its current values."""
	_load_implementations()

	resolved_type = provider_type or (
		settings.server_provider if settings else frappe.get_single_value("Atlas Settings", "server_provider")
	)
	provider_class = _REGISTRY.get(resolved_type)
	if provider_class is None:
		frappe.throw(f"No implementation for server provider type {resolved_type!r}")

	return provider_class(settings)


def _load_implementations() -> None:
	import atlas.atlas.core.server_providers.scaleway


__all__ = ["ServerProvider", "get_server_provider", "register"]
