from __future__ import annotations

from typing import Any

import frappe

from atlas.api.core.base import ApiResult, ListQuery, Page, add_tag_filter, build_page
from atlas.api.core.docs import api_docs
from atlas.api.models import HostAccessGrantPayload, HostAccessResponse, HostAccessRevokePayload, HostResponse
from atlas.api.router import hosts
from atlas.atlas.core.tags import read_tags_for
from atlas.auth.identity import require_tenant_zero_operator
from atlas.service.core.warpgate.access import HostAccess


@hosts.get("")
@api_docs()
def list_hosts(query: ListQuery) -> Page[HostResponse]:
	"""List hosts.

	Returns the Metal hosts that are not deleted, with their tags, in title order. Pass `tag` to narrow them.
	"""
	require_tenant_zero_operator()
	filters: dict[str, Any] = {"status": ["!=", "Deleted"]}
	if not add_tag_filter("Metal Server", query, filters):
		return build_page([], query)
	rows = frappe.get_all(
		"Metal Server",
		filters=filters,
		fields=["name", "title", "status"],
		order_by="title asc",
		offset=query.offset,
		limit=query.fetch_limit,
	)
	tags = read_tags_for("Metal Server", [row.name for row in rows])
	return build_page(
		[
			HostResponse(id=row.name, title=row.title, status=row.status.lower(), tags=tags[row.name])
			for row in rows
		],
		query,
	)


@hosts.post("<host_id>/access/grant")
@api_docs(
	request_example={"email": "alice@frappe.io", "expires_at": "2026-10-02T14:00:00Z"},
	responses={
		200: {
			"description": "The person can open the host until expires_at. A repeated grant moves the end time."
		},
		404: {"description": "The host does not exist."},
		503: {"description": "This region has no Warpgate, or it did not answer. Retry later."},
	},
)
def grant_host_access(host_id: str, payload: HostAccessGrantPayload) -> HostAccessResponse:
	"""Grant host access.

	Opens one host, or every host when the ID is `all`, to one person until `expires_at`. Atlas registers the person in Warpgate when needed, so they can sign in with Central afterwards.
	"""
	require_tenant_zero_operator()
	email = HostAccess(host_id).grant(payload.email, payload.expires_at)
	return HostAccessResponse(host_id=host_id, email=email, expires_at=payload.expires_at)


@hosts.post("<host_id>/access/revoke")
@api_docs(
	request_example={"email": "alice@frappe.io"},
	responses={
		204: {
			"description": "The person can no longer open the host, and has no live session to it. Revoking twice is safe."
		},
		404: {"description": "The host does not exist."},
		503: {"description": "This region has no Warpgate, or it did not answer. Retry later."},
	},
)
def revoke_host_access(host_id: str, payload: HostAccessRevokePayload) -> ApiResult[None]:
	"""Revoke host access.

	Closes one host, or every host when the ID is `all`, to one person now, and ends the person's live sessions to the hosts they can no longer open.
	"""
	require_tenant_zero_operator()
	HostAccess(host_id).revoke(payload.email)
	return ApiResult(None, status=204)
