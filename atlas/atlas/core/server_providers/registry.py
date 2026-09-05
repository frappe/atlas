from __future__ import annotations

from typing import TYPE_CHECKING

import frappe

from atlas.atlas.core.server_providers.base import ServerProvider

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

_REGISTRY: dict[str, type[ServerProvider]] = {}


def register(provider_class: type[ServerProvider]) -> type[ServerProvider]:
	"""Register one server provider class by its stable type."""
	if not provider_class.provider_type:
		raise ValueError("A server provider type cannot be empty")
	if provider_class.provider_type in _REGISTRY:
		raise ValueError(f"A server provider is already registered for {provider_class.provider_type}")

	_REGISTRY[provider_class.provider_type] = provider_class
	return provider_class


def get_server_provider(
	provider_type: str | None = None, settings: "AtlasSettings | None" = None
) -> ServerProvider:
	"""Return the selected server provider."""
	load_implementations()

	resolved_type = provider_type or (
		settings.server_provider if settings else frappe.get_single_value("Atlas Settings", "server_provider")
	)
	provider_class = _REGISTRY.get(resolved_type)
	if provider_class is None:
		raise ValueError(f"No implementation exists for server provider type {resolved_type!r}")

	return provider_class(settings)


def load_implementations() -> None:
	"""Load the built-in server provider implementations."""
	import atlas.atlas.core.server_providers.scaleway
