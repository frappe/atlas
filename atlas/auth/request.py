from __future__ import annotations

import frappe

from atlas.auth.identity import (
	CENTRAL_TENANT,
	SESSION_ISSUER,
	AtlasIdentity,
	current_identity,
	set_identity,
)
from atlas.auth.roles import has_role
from atlas.auth.token import TokenValidator
from atlas.auth.user import ensure_tenant_user

ATLAS_API_PREFIX = "/api/atlas"
ATLAS_DOCS_PREFIX = "/api/atlas/docs"
REALTIME_PREFIX = "/socket.io"
REALTIME_PATHS = frozenset(
	{
		"/api/method/frappe.realtime.get_user_info",
		"/api/method/frappe.realtime.has_permission",
	}
)
GUEST_PATHS = frozenset({"/login", "/api/method/login", "/api/method/logout"})
PUBLIC_ATLAS_PATHS = frozenset({"/api/atlas/jwks.json"})
GUEST_PATH_PREFIXES = ("/assets/",)


def validate_auth() -> None:
	"""Accept a service token, allow realtime support paths, and enforce route access."""
	set_identity(None)
	authenticate_token()
	path = frappe.request.path.rstrip("/") or "/"
	if is_realtime_path(path) or path in PUBLIC_ATLAS_PATHS:
		return

	if frappe.session.user in ("", "Guest"):
		validate_guest_path(path)
		return

	is_system_manager = has_role("System Manager")
	if is_path_within(path, ATLAS_API_PREFIX):
		if is_system_manager:
			set_identity(
				AtlasIdentity(
					subject=frappe.session.user,
					issuer=SESSION_ISSUER,
					tenant=CENTRAL_TENANT,
					scope="*",
				)
			)
		elif current_identity() is None:
			raise frappe.PermissionError
		return

	if is_system_manager:
		return

	if has_role("Atlas Admin"):
		raise frappe.PermissionError


def validate_guest_path(path: str) -> None:
	"""Allow a guest to sign in and to read the API reference, and nothing else."""
	if path in GUEST_PATHS or path.startswith(GUEST_PATH_PREFIXES):
		return

	if is_path_within(path, ATLAS_DOCS_PREFIX):
		return

	raise frappe.PermissionError


def is_realtime_path(path: str) -> bool:
	"""Return whether a path serves Socket.IO or its web-process permission calls."""
	return path in REALTIME_PATHS or is_path_within(path, REALTIME_PREFIX)


def is_path_within(path: str, prefix: str) -> bool:
	"""Return whether a path is the prefix or one of its children."""
	return path == prefix or path.startswith(f"{prefix}/")


def authenticate_token() -> None:
	"""Sign in as the Atlas API user when a service token is valid."""
	if frappe.session.user not in ("", "Guest"):
		return

	scheme, _, token = (frappe.get_request_header("Authorization") or "").partition(" ")
	if scheme.lower() != "bearer":
		return
	if not token:
		return

	claims = TokenValidator().claims(token)
	if claims is None:
		return

	identity = AtlasIdentity.from_claims(claims)
	if identity is None:
		return

	set_identity(identity)
	frappe.set_user(ensure_tenant_user(identity.tenant))  # nosemgrep
