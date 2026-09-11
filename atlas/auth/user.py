from __future__ import annotations

import frappe

from atlas.auth.identity import CENTRAL_TENANT

CENTRAL_TENANT_USER = "tenant-all@atlas.local"
ATLAS_ADMIN_ROLE = "Atlas Admin"


def ensure_tenant_user(tenant: str) -> str:
	"""Return the user of one tenant claim, and create it on first use."""
	if tenant == CENTRAL_TENANT:
		name, first_name = CENTRAL_TENANT_USER, "Every Tenant"
	else:
		tenant_id = int(tenant)
		name, first_name = f"tenant-{tenant_id}@atlas.local", f"Tenant {tenant_id}"

	if frappe.db.exists("User", name):
		return name

	try:
		_insert_api_user(name, first_name)
		# The authentication hook runs before any route work, so this commit holds only the new
		# user. Frappe rolls a read request back, and the user must outlive the request.
		frappe.db.commit()  # nosemgrep
	except frappe.DuplicateEntryError:
		frappe.db.rollback()

	return name


def _insert_api_user(name: str, first_name: str) -> None:
	frappe.get_doc(
		{
			"doctype": "User",
			"name": name,
			"email": name,
			"first_name": first_name,
			"user_type": "Website User",
			"enabled": 1,
			"send_welcome_email": 0,
			"roles": [{"role": ATLAS_ADMIN_ROLE}],
		}
	).insert(ignore_permissions=True)
