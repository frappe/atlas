"""Typed accessors for the `Atlas Settings` Single.

`get_provider()` / `get_ssh_key()` / `get_ssh_private_key_path()` /
`provision()` are the indirection layer the spec describes: callers never
read the Single directly, and they never branch on `provider_type`.

These helpers also re-export through `atlas/__init__.py` so the
canonical call is `atlas.get_provider()`.
"""

from __future__ import annotations

import frappe
from frappe import _

from atlas.atlas import providers
from atlas.atlas.providers.base import Provider, ProvisionRequest, ProvisionResult, SshKey


def get_provider() -> Provider:
	name = frappe.get_single("Atlas Settings").provider
	if not name:
		frappe.throw(_("Atlas Settings has no active provider; set one before provisioning"))
	return providers.for_provider(name)


def get_ssh_key() -> SshKey:
	settings = frappe.get_single("Atlas Settings")
	return SshKey(
		vendor_id=settings.ssh_key_id or None,
		public_key=settings.ssh_public_key or None,
	)


def get_ssh_private_key_path() -> str:
	path = frappe.db.get_single_value("Atlas Settings", "ssh_private_key_path")
	if not path:
		frappe.throw(_("Atlas Settings has no ssh_private_key_path; cannot SSH"))
	return path


def provision(request: ProvisionRequest) -> ProvisionResult:
	return get_provider().provision(request)
