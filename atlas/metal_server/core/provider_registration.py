from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

import frappe

from atlas.atlas.core.server_providers.base import UnsupportedProviderOperation

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


def register_server(server: MetalServer) -> MetalServer:
	"""Import an existing provider host and insert or reuse its active record."""
	provider = server.settings.server_provider_controller
	if not provider.is_registration_only:
		raise UnsupportedProviderOperation("registration without provisioning")
	provider.validate_settings()
	provider.import_server(server)
	lock_name = (
		"host-register:"
		+ sha256(f"{frappe.db.cur_db_name}:{server.provider_server_id}".encode()).hexdigest()[:32]
	)

	with frappe.db.advisory_lock(lock_name):
		# Discovery finishes before the lock; the lookup sees the last committed registration.
		frappe.db.rollback()  # nosemgrep
		name = frappe.db.get_value(
			"Metal Server", {"provider_server_id": server.provider_server_id, "status": ["!=", "Deleted"]}
		)
		if name:
			server = frappe.get_doc("Metal Server", name)
		else:
			server.insert()
		frappe.db.commit()  # nosemgrep
	return server
