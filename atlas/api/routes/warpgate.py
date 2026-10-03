from __future__ import annotations

from atlas.api.core.base import ApiResult
from atlas.api.core.docs import api_docs
from atlas.api.models import WarpgateSessionsClosePayload
from atlas.api.router import warpgate
from atlas.auth.identity import require_tenant_zero_operator
from atlas.service.core.warpgate.access import close_sessions


@warpgate.post("sessions/close")
@api_docs(
	request_example={"email": "alice@frappe.io"},
	responses={
		204: {
			"description": "The person has no live session. Closing twice, or in a region without Warpgate, is safe."
		},
		503: {"description": "Warpgate did not answer. Retry later."},
	},
)
def close_warpgate_sessions(payload: WarpgateSessionsClosePayload) -> ApiResult[None]:
	"""Close Warpgate sessions.

	Ends every live Warpgate session of one person now. Roles stay as they are.
	"""
	require_tenant_zero_operator()
	close_sessions(payload.email)
	return ApiResult(None, status=204)
