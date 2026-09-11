from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import frappe
import orjson
from werkzeug.exceptions import HTTPException
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request, Response

from atlas.auth.identity import CENTRAL_TENANT, TENANT_HEADER, AtlasIdentity, set_identity

TENANT_ID = 7
OTHER_TENANT_ID = 8


@contextmanager
def api_request(
	method: str = "GET",
	path: str = "/",
	tenant_id: int | str | None = None,
	send_tenant_header: bool = False,
	**builder_options: Any,
) -> Generator[Request]:
	"""Run a block with frappe.local.request set and the in_test shortcut disabled."""
	previous_request = getattr(frappe.local, "request", None)
	previous_identity = getattr(frappe.local, "atlas_identity", None)
	previous_flag = frappe.flags.in_test
	headers = dict(builder_options.pop("headers", {}))
	if tenant_id is not None and send_tenant_header:
		headers[TENANT_HEADER] = str(tenant_id)

	environ = EnvironBuilder(method=method, path=path, headers=headers, **builder_options).get_environ()
	frappe.local.request = Request(environ)
	set_identity(
		AtlasIdentity(
			subject="test",
			issuer="central",
			tenant=str(tenant_id) if tenant_id is not None else CENTRAL_TENANT,
			scope="*",
		)
	)
	frappe.flags.in_test = False
	try:
		yield frappe.local.request
	finally:
		frappe.local.request = previous_request
		set_identity(previous_identity)
		frappe.flags.in_test = previous_flag


def call_route(handler, *args, **kwargs) -> tuple[int, Any]:
	"""Return the status code and the decoded body of one route call."""
	try:
		response = handler(*args, **kwargs)
	except HTTPException as exception:
		response = exception.response

	assert isinstance(response, Response)
	body = response.get_data()
	return response.status_code, orjson.loads(body) if body else None


def error_body(handler, *args, **kwargs) -> tuple[int, dict]:
	"""Call a handler that fails and return the status code and the error body."""
	status, body = call_route(handler, *args, **kwargs)
	if "error" not in (body or {}):
		raise AssertionError("handler did not return an error body")

	return status, body
