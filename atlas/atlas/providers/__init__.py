"""Provider abstraction registry.

Vendors register their `Provider` subclass via `@register`. Callers ask for an
instance via `for_provider_type(provider_type)`, which maps the type to its
registered implementation class. There is no `Provider` DocType row to load: the
active vendor is `Atlas Settings.provider_type` (read by `atlas.get_provider()`),
and a historical Server carries its own `provider_type`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frappe

if TYPE_CHECKING:
	from atlas.atlas.providers.base import Provider


_REGISTRY: dict[str, type["Provider"]] = {}


def register(cls: type["Provider"]) -> type["Provider"]:
	"""Class decorator that records `cls` against its `provider_type`."""
	_REGISTRY[cls.provider_type] = cls
	return cls


def for_provider_type(provider_type: str) -> "Provider":
	"""Return an instantiated `Provider` for the given `provider_type`.

	Raises `frappe.ValidationError` if the type has no registered implementation.
	"""
	_load_implementations()
	factory = _REGISTRY.get(provider_type)
	if factory is None:
		frappe.throw(f"No implementation for provider_type {provider_type!r}")
	return factory()


def _load_implementations() -> None:
	"""Import vendor modules so their `@register` decorators run.

	Idempotent — Python caches the import. Kept in a separate function so
	tests that stub the registry can avoid pulling DO/Self-Managed in.
	"""
	# Avoid circular imports at module-import time.
	import atlas.atlas.providers.digitalocean
	import atlas.atlas.providers.fake
	import atlas.atlas.providers.scaleway
	import atlas.atlas.providers.self_managed
