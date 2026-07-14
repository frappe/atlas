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
	provider_type = frappe.db.get_single_value("Atlas Settings", "provider_type")
	if not provider_type:
		frappe.throw(_("Atlas Settings has no provider_type; set one before provisioning"))
	return providers.for_provider_type(provider_type)


def get_ssh_key() -> SshKey:
	"""The Atlas keypair, with the active vendor's handle for it. The public key
	is vendor-agnostic and lives on Atlas Settings; `vendor_id` is the vendor's
	own id for the uploaded key (DO key id / fingerprint, Scaleway IAM id) and so
	lives on the active vendor's Settings — a DO key id is meaningless to Scaleway.
	Vendors that take IPs directly (Self-Managed) or have no API (Fake) carry no
	such field, so `vendor_id` is simply None for them."""
	settings = frappe.get_single("Atlas Settings")
	return SshKey(
		vendor_id=_active_vendor_ssh_key_id(settings.provider_type),
		public_key=settings.ssh_public_key or None,
	)


def _active_vendor_ssh_key_id(provider_type: str) -> str | None:
	vendor_single = {
		"DigitalOcean": "DigitalOcean Settings",
		"Scaleway": "Scaleway Settings",
	}.get(provider_type)
	if not vendor_single:
		return None
	return frappe.db.get_single_value(vendor_single, "ssh_key_id") or None


def get_ssh_private_key_path() -> str:
	path = frappe.db.get_single_value("Atlas Settings", "ssh_private_key_path")
	if not path:
		frappe.throw(_("Atlas Settings has no ssh_private_key_path; cannot SSH"))
	return path


def satellite_public_keys() -> list[str]:
	"""OpenSSH public keys of the Satellite orchestrator(s) that manage this Atlas's
	VMs (spec/28). Atlas is a pure provisioner; it injects these into every host (at
	bootstrap) and guest (at provision) authorized_keys so a Satellite can SSH the bare
	box it is handed. Configured one-per-line on Atlas Settings; empty on an Atlas with
	no Satellite, in which case injection is a no-op."""
	raw = frappe.db.get_single_value("Atlas Settings", "satellite_public_keys") or ""
	return [line.strip() for line in raw.splitlines() if line.strip()]


def provision(request: ProvisionRequest) -> ProvisionResult:
	return get_provider().provision(request)
