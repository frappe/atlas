from __future__ import annotations

from typing import Any

import frappe

from atlas.api.core.errors import InvalidRequest
from atlas.auth.identity import current_identity, get_current_tenant_id
from atlas.auth.roles import has_role

TENANT_DOCUMENT_TYPES = {
	"Metal Server IP Address",
	"Virtual Machine",
	"Virtual Machine Image",
}


def is_document_visible(document: Any, ptype: str = "read") -> bool:
	"""Return whether the tenant of the current request may use one Atlas document."""
	if document.doctype not in TENANT_DOCUMENT_TYPES:
		return False

	tenant_id = _request_tenant_id()
	if tenant_id is None:
		return False

	if document.doctype == "Virtual Machine Image" and ptype == "read":
		return document.is_visible_to_tenant(tenant_id)

	return document.tenant_id == tenant_id


def get_permission_query_conditions(user: str | None = None, doctype: str | None = None) -> str:
	"""Return the list condition for one Atlas DocType."""
	tenant_id = _request_tenant_id()
	if tenant_id is None:
		return "" if current_identity() is None and has_role("System Manager", user) else "1=0"
	if doctype not in TENANT_DOCUMENT_TYPES:
		return "1=0"

	condition = f"`tab{doctype}`.`tenant_id` = {tenant_id}"
	if doctype == "Virtual Machine Image":
		return f"({condition} OR `tabVirtual Machine Image`.`image_type` = 'system')"
	return condition


def has_permission(doc: Any, ptype: str, user: str | None = None, debug: bool = False) -> bool:
	"""Return whether a user can access one Atlas document."""
	if _request_tenant_id() is None:
		return current_identity() is None and has_role("System Manager", user)

	return is_document_visible(doc, ptype)


def _request_tenant_id() -> int | None:
	try:
		return get_current_tenant_id()
	except InvalidRequest, frappe.PermissionError:
		return None
