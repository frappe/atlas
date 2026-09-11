from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import frappe

from atlas.api.core.errors import InvalidRequest

TENANT_HEADER = "X-Tenant-ID"
MAXIMUM_TENANT_ID = 0xFFFFFFFF
CENTRAL_TENANT = "*"
SESSION_ISSUER = "session"


@dataclass(frozen=True, slots=True)
class AtlasIdentity:
	"""The verified caller of one Atlas API request."""

	subject: str
	issuer: str
	tenant: str
	scope: str

	@classmethod
	def from_claims(cls, claims: dict[str, Any]) -> AtlasIdentity | None:
		"""Return the identity of validated token claims, or None when the tenant is not usable."""
		subject = claims.get("sub")
		issuer = claims.get("iss")
		tenant = claims.get("tenant")
		scope = claims.get("scope")
		if not all(isinstance(value, str) and value for value in (subject, issuer, tenant, scope)):
			return None

		if tenant != CENTRAL_TENANT:
			try:
				parse_tenant_id(tenant)
			except InvalidRequest:
				return None

		return cls(subject=subject, issuer=issuer, tenant=tenant, scope=scope)

	@property
	def is_central(self) -> bool:
		"""Return whether the caller acts for every tenant."""
		return self.tenant == CENTRAL_TENANT


def set_identity(identity: AtlasIdentity | None) -> None:
	"""Store the identity of the current request, or None to forget the previous one."""
	frappe.local.atlas_identity = identity


def current_identity() -> AtlasIdentity | None:
	"""Return the identity of the current request, or None outside an Atlas API request."""
	return getattr(frappe.local, "atlas_identity", None)


def get_current_tenant_id() -> int:
	"""Return the tenant that the current request acts for."""
	identity = current_identity()
	if identity is None:
		raise frappe.PermissionError

	request = getattr(frappe.local, "request", None)
	header = request.headers.get(TENANT_HEADER, "") if request else ""
	if identity.is_central:
		return parse_tenant_id(header)

	if header.strip() and parse_tenant_id(header) != int(identity.tenant):
		raise invalid_tenant("The tenant ID does not match the service credential.")

	return int(identity.tenant)


def parse_tenant_id(value: str) -> int:
	"""Return one valid tenant ID."""
	if not value.strip():
		raise invalid_tenant("The request needs a tenant ID.")
	try:
		tenant_id = int(value.strip())
	except ValueError as error:
		raise invalid_tenant("The tenant ID must be a whole number.") from error

	if not 0 <= tenant_id <= MAXIMUM_TENANT_ID:
		raise invalid_tenant("The tenant ID must be an unsigned 32-bit integer.")
	return tenant_id


def invalid_tenant(message: str) -> InvalidRequest:
	"""Return the failure for one unusable tenant value."""
	return InvalidRequest(message, fields=[{"name": TENANT_HEADER, "message": message}])
