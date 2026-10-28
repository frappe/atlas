from __future__ import annotations

import frappe

from atlas.api.core.base import ApiResult
from atlas.api.core.docs import api_docs
from atlas.api.models import InvestigationPayload, InvestigationResponse
from atlas.api.router import get_resource_location, investigations
from atlas.auth.identity import require_tenant_zero_operator
from atlas.investigation.doctype.investigation.investigation import Investigation


@investigations.post("")
@api_docs(
	request_example={
		"problem": "VM vm-0000076 does not boot after a restart.",
		"context": {"virtual_machine": "vm-0000076"},
		"requested_by": "alice@example.com",
	},
	responses={
		201: {
			"description": "Atlas queued the investigation. Read it until the status is completed or failed."
		}
	},
)
def create_investigation(payload: InvestigationPayload) -> ApiResult[InvestigationResponse]:
	"""Start investigation.

	Queues the read-only investigator on one problem. It reads telemetry, records, and hosts, and changes nothing.
	"""
	require_tenant_zero_operator()
	investigation = Investigation.create_for_central(
		payload.problem, payload.title, payload.context, payload.requested_by
	)
	return ApiResult(
		InvestigationResponse.from_document(investigation),
		status=201,
		headers={"Location": get_resource_location("investigations", investigation.name)},
	)


@investigations.get("<investigation_id>")
@api_docs(responses={404: {"description": "The investigation does not exist."}})
def get_investigation(investigation_id: str) -> InvestigationResponse:
	"""Get investigation.

	Returns the status, the report, and the audit log of tool calls. Atlas masks credentials.
	"""
	require_tenant_zero_operator()
	if not frappe.db.exists("Investigation", investigation_id):
		raise frappe.DoesNotExistError
	return InvestigationResponse.from_document(frappe.get_doc("Investigation", investigation_id))
