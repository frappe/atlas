"""TLS provider registry — twin of `atlas/atlas/providers/__init__.py`.

Issuers register their `TlsProvider` subclass via `@register`. Callers ask for an
instance via `for_tls_provider_type(provider_type)`, which maps the type to its
registered implementation class. There is no `TLS Provider` DocType row to load:
the active issuer is `Atlas Settings.tls_provider_type`, and a `Root Domain` /
`TLS Certificate` carries its own `tls_provider_type`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frappe

if TYPE_CHECKING:
	from atlas.atlas.tls.base import TlsProvider


_REGISTRY: dict[str, type["TlsProvider"]] = {}


def register(cls: type["TlsProvider"]) -> type["TlsProvider"]:
	"""Class decorator that records `cls` against its `provider_type`."""
	_REGISTRY[cls.provider_type] = cls
	return cls


def for_tls_provider_type(provider_type: str) -> "TlsProvider":
	"""Return an instantiated `TlsProvider` for the given `provider_type`.

	Raises `frappe.ValidationError` if the type has no registered implementation.
	"""
	_load_implementations()
	factory = _REGISTRY.get(provider_type)
	if factory is None:
		frappe.throw(f"No implementation for provider_type {provider_type!r}")
	return factory()


def _load_implementations() -> None:
	"""Import issuer modules so their `@register` decorators run. Idempotent."""
	import atlas.atlas.tls.letsencrypt
	import atlas.atlas.tls.self_managed
	import atlas.atlas.tls.zerossl
