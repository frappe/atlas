from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import frappe

from atlas.atlas.core.server_providers.base import ProviderOperationError, ServerProvider
from atlas.atlas.core.ssh import wait_for_server
from atlas.server.core.host_installation import HostInstallation

if TYPE_CHECKING:
	from atlas.server.doctype.server.server import Server


class ServerProvisioner:
	"""Own the safe server setup sequence and its durable progress."""

	setup_fields = (
		"status",
		"is_provisioning_completed",
		"provider_server_id",
		"provider_metadata",
		"public_ipv4_address",
		"private_ipv4_address",
		"public_network_interface",
		"private_network_interface",
		"wireguard_ip_address",
		"wireguard_public_key",
	)
	ssh_timeout_seconds = 1_000
	ssh_poll_interval_seconds = 1

	def __init__(self, server: "Server", provider: ServerProvider | None = None) -> None:
		self.server = server
		self.provider = provider or server.settings.server_provider_controller
		self.host_installation = HostInstallation(server)
		self.logger = logging.getLogger("atlas.server.provisioning")

	def run(self) -> None:
		"""Run each server setup step in order."""
		phase = "start"
		self.server.status = "Installing"
		self.save_progress()
		try:
			for phase, operation in self.steps:
				self.run_step(phase, operation)
			self.server.status = "Running"
			self.server.is_provisioning_completed = 1
			self.save_progress()
		except Exception:
			self.server.status = "Failed"
			self.save_progress()
			self.logger.exception(
				"Server provisioning failed",
				extra={"resource": self.server.name, "operation": "provision", "phase": phase},
			)
			frappe.log_error(title=f"Server provisioning failed for {self.server.name} during {phase}")
			raise

	@property
	def steps(self) -> tuple[tuple[str, Callable[[], None]], ...]:
		"""Return the server setup steps in execution order."""
		return (
			("provider-preparation", lambda: self.provider.prepare_server(self.server)),
			("secure-shell", self.wait_for_root_ssh),
			("provider-network", lambda: self.provider.configure_server_network(self.server)),
			("wireguard", self.host_installation.configure_wireguard),
			("metal", self.host_installation.install_metal),
		)

	def run_step(self, phase: str, operation: Callable[[], None]) -> None:
		"""Run one setup step and save its resulting Server fields."""
		self.logger.info(
			"Server provisioning step started",
			extra={"resource": self.server.name, "operation": "provision", "phase": phase},
		)
		operation()
		self.save_progress()

	def wait_for_root_ssh(self) -> None:
		"""Wait until the root Secure Shell account is available."""
		if not self.server.public_ipv4_address:
			raise ProviderOperationError("Server has no public IPv4 address")

		try:
			user = wait_for_server(
				host=self.server.public_ipv4_address,
				users=self.provider.ssh_users,
				timeout_seconds=self.ssh_timeout_seconds,
				poll_interval_seconds=self.ssh_poll_interval_seconds,
			)
		except TimeoutError as error:
			raise ProviderOperationError(str(error), is_retryable=True) from error
		if user == "root":
			return

		self.provider.promote_ssh_user(self.server, user)
		try:
			root_user = wait_for_server(
				host=self.server.public_ipv4_address,
				users=("root",),
				timeout_seconds=self.ssh_timeout_seconds,
				poll_interval_seconds=self.ssh_poll_interval_seconds,
			)
		except TimeoutError as error:
			raise ProviderOperationError(
				"Root Secure Shell access did not become ready after user promotion",
				is_retryable=True,
			) from error
		if root_user != "root":
			raise ProviderOperationError(
				"Root Secure Shell access did not become ready after user promotion",
				is_retryable=True,
			)

	def save_progress(self) -> None:
		"""Store the current setup fields and commit the setup transaction."""
		self.server.db_set({field: self.server.get(field) for field in self.setup_fields})
		frappe.db.commit()  # nosemgrep
