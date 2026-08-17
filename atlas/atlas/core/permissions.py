"""Operator-role helper.

Atlas is operator/Central-facing (System Manager); the end-user `Atlas User` role,
owner-scoping, and the signup surface were retired now that Central is the front
door (spec/README.md, spec/16-central.md). The one thing that survived that refactor
is the operator check the firewall + VPN-broker APIs gate on — kept here as the single
source of truth so `atlas.atlas.api.firewall` / `atlas.atlas.api.tunnel` have a stable
import.
"""

import frappe

OPERATOR_ROLE = "System Manager"


def _is_operator(user: str) -> bool:
	"""True if `user` is an Atlas operator (System Manager)."""
	return OPERATOR_ROLE in frappe.get_roles(user)
