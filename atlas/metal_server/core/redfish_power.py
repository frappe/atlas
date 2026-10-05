from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

import frappe
from frappe.utils import now_datetime

from atlas.atlas.core.server_providers.redfish.client import RedfishError, RedfishPowerStatus
from atlas.atlas.core.server_providers.redfish.provider import RedfishProvider

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


class RedfishPower:
	"""Serialize BMC observations and store them on the owning Metal Server."""

	def __init__(self, server: MetalServer) -> None:
		self.server = server
		self.provider = server.settings.server_provider_controller
		if not isinstance(self.provider, RedfishProvider):
			raise RedfishError("Select the Redfish server provider to use BMC power operations")

	def refresh(self) -> None:
		"""Refresh power and health without promoting the host to Running."""
		lock_name = (
			"redfish-power:" + sha256(f"{frappe.db.cur_db_name}:{self.server.name}".encode()).hexdigest()[:32]
		)
		with frappe.db.advisory_lock(lock_name):
			frappe.db.rollback()  # nosemgrep
			self.server.reload()
			self.server._validate_power_action()
			status = self.provider.read_power_status(self.server._provider_server_id())
			self._apply_status(status)
			frappe.db.commit()  # nosemgrep

	def _apply_status(self, status: RedfishPowerStatus) -> None:
		lifecycle_status = self.server.status
		if status.power_state == "Off":
			lifecycle_status = "Stopped"
		elif lifecycle_status == "Stopped" or (status.power_state != "On" and lifecycle_status == "Running"):
			lifecycle_status = "Pending"
		self.server.db_set(
			{
				"redfish_power_state": status.power_state,
				"redfish_health": status.health,
				"redfish_power_updated_on": now_datetime(),
				"status": lifecycle_status,
			}
		)
