from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

import frappe

from atlas.atlas.core.server_providers.base import ServerPowerAction, ServerPowerStatus

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


class ServerPower:
	"""Serialize BMC power operations and reconcile the Metal Server lifecycle."""

	def __init__(self, server: MetalServer) -> None:
		self.server = server
		self.provider = server.settings.server_provider_controller

	def refresh(self) -> None:
		"""Refresh power and health without promoting the host to Running."""
		self._run()

	def set_power_state(self, action: ServerPowerAction) -> None:
		"""Apply one power action and reconcile lifecycle after an observed result."""
		if not self.provider.is_registration_only:
			self.provider.set_power_state(self.server._provider_server_id(), action)
			if action == ServerPowerAction.STOP:
				self.server.db_set("status", "Stopped")
			elif action == ServerPowerAction.START and self.server.is_provisioning_completed:
				self.server.db_set("status", "Running")
			return
		self._run(action)

	def _run(self, action: ServerPowerAction | None = None) -> None:
		lock_name = (
			"server-power:" + sha256(f"{frappe.db.cur_db_name}:{self.server.name}".encode()).hexdigest()[:32]
		)
		with frappe.cache.lock(lock_name, timeout=300, blocking_timeout=10):
			frappe.db.rollback()  # nosemgrep
			self.server.reload()
			self.server._validate_power_action()
			if action is not None:
				self.provider.set_power_state(self.server._provider_server_id(), action)
				if action == ServerPowerAction.STOP:
					return
			status = self.provider.read_power_status(self.server._provider_server_id())
			frappe.db.rollback()  # nosemgrep
			frappe.db.get_value(self.server.doctype, self.server.name, "status", for_update=True)
			self.server.reload()
			self.server._validate_power_action()
			self._apply_status(status, action)
			frappe.db.commit()  # nosemgrep

	def _apply_status(self, status: ServerPowerStatus, action: ServerPowerAction | None) -> None:
		lifecycle_status = self.server.status
		if status.power_state == "Off":
			lifecycle_status = "Stopped"
		elif lifecycle_status == "Stopped" or (
			lifecycle_status == "Running"
			and (status.power_state != "On" or action == ServerPowerAction.REBOOT)
		):
			lifecycle_status = "Pending"
		self.server.db_set({"status": lifecycle_status})
