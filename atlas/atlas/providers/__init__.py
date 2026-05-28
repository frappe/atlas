"""Provider abstraction registry.

Vendors register their `Provider` subclass via `@register`. Callers ask for an
instance via `for_provider(provider_name)`, which looks up the `Provider`
DocType row, checks `is_active`, and instantiates the registered class.
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


def for_provider(provider_name: str) -> "Provider":
	"""Return an instantiated `Provider` for the given `Provider` row.

	Raises `frappe.ValidationError` if the row is archived (`is_active=0`)
	or if its `provider_type` has no registered implementation.
	"""
	_load_implementations()
	row = frappe.get_doc("Provider", provider_name)
	if not row.is_active:
		frappe.throw(f"Provider {provider_name!r} is archived")
	factory = _REGISTRY.get(row.provider_type)
	if factory is None:
		frappe.throw(f"No implementation for provider_type {row.provider_type!r}")
	return factory()


def _load_implementations() -> None:
	"""Import vendor modules so their `@register` decorators run.

	Idempotent — Python caches the import. Kept in a separate function so
	tests that stub the registry can avoid pulling DO/Self-Managed in.
	"""
	# Avoid circular imports at module-import time.
	import atlas.atlas.providers.digitalocean  # noqa: F401
	import atlas.atlas.providers.self_managed  # noqa: F401
