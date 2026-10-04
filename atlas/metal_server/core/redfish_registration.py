from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

import frappe

from atlas.atlas.core.server_providers.redfish.client import RedfishClient, RedfishError
from atlas.atlas.core.server_providers.redfish.provider import RedfishProvider

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


def register_server(url: str, username: str = "", password: str = "") -> MetalServer:
	"""Register an existing Redfish system without provisioning it."""
	settings = frappe.get_single("Atlas Settings")
	provider = settings.server_provider_controller
	if not isinstance(provider, RedfishProvider):
		raise RedfishError("Select Redfish as the server provider in Atlas Settings before registration")
	provider.validate_settings()
	system = RedfishClient(url, username, password).discover_system()
	provider_server_id = "redfish-" + sha256(system.url.encode()).hexdigest()[:32]
	lock_name = (
		"redfish-register:" + sha256(f"{frappe.db.cur_db_name}:{system.url}".encode()).hexdigest()[:32]
	)

	with frappe.db.advisory_lock(lock_name):
		# Read after acquiring the lock, so concurrent registration sees the committed record.
		frappe.db.rollback()  # nosemgrep
		name = frappe.db.get_value(
			"Metal Server", {"provider_server_id": provider_server_id, "status": ["!=", "Deleted"]}
		)
		if name:
			server: MetalServer = frappe.get_doc("Metal Server", name)
		else:
			server = frappe.new_doc("Metal Server")
			server._redfish_registration = system
			server.redfish_url = system.url
			server.redfish_username = username
			server.redfish_password = password
			server.provider_server_id = provider_server_id
			server.provider_metadata = frappe.as_json(
				{"id": system.id, "name": system.name, "uuid": system.uuid}
			)
			server.status = "Pending"
			server.insert()
		# Hold the lock until the insert is visible to the next request.
		frappe.db.commit()  # nosemgrep
	return server
