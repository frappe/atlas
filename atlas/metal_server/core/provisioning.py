from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import frappe

from atlas.atlas.core.server_providers.base import (
	ProviderOperationError,
	ServerProvider,
	UnsupportedProviderOperation,
)
from atlas.atlas.core.ssh import wait_for_server
from atlas.metal_server.core.atlas_peer import AtlasPeer
from atlas.metal_server.core.host_installation import HostInstallation

if TYPE_CHECKING:
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


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
		"private_network_mac_address",
		"wireguard_ip_address",
		"wireguard_public_key",
	)
	ssh_timeout_seconds = 1_000
	wireguard_timeout_seconds = 120
	ssh_poll_interval_seconds = 1

	def __init__(self, server: "MetalServer", provider: ServerProvider | None = None) -> None:
		self.server = server
		self.provider = provider or server.settings.server_provider_controller
		self.host_installation = HostInstallation(server)
		self.logger = logging.getLogger("atlas.metal_server.provisioning")

	def run(self) -> None:
		"""Run each server setup step in order."""
		if self.provider.is_registration_only:
			raise UnsupportedProviderOperation("server provisioning")

		phase = "start"
		self.server.status = "Installing"
		self.save_progress()
		try:
			for phase, operation in self.steps:
				self.run_step(phase, operation)
			self.server.status = "Running"
			self.server.is_provisioning_completed = 1
			self.save_progress()
			self.server.enqueue_disk_sync()
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
			("provider-create", self.server.ensure_provider_server),
			("provider-preparation", lambda: self.provider.prepare_server(self.server)),
			("secure-shell", self.wait_for_root_ssh),
			("provider-network", lambda: self.provider.configure_server_network(self.server)),
			("wireguard", self.host_installation.configure_wireguard),
			("wireguard-link", self.wait_for_wireguard_ssh),
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
		"""Wait until the root Secure Shell account is available. A retry after WireGuard setup uses wg0."""
		if not self.server.wireguard_public_key and not self.server.public_ipv4_address:
			raise ProviderOperationError("Server has no public IPv4 address")

		try:
			user = wait_for_server(
				host=self.server.ssh_host,
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
				host=self.server.ssh_host,
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

	def wait_for_wireguard_ssh(self) -> None:
		"""Wait until root Secure Shell answers on the host wg0 address."""
		# Publish the new host peer now instead of waiting for the scheduler.
		AtlasPeer(self.server.settings).write_config()
		try:
			wait_for_server(
				host=self.server.ssh_host,
				users=("root",),
				timeout_seconds=self.wireguard_timeout_seconds,
				poll_interval_seconds=self.ssh_poll_interval_seconds,
			)
		except TimeoutError as error:
			raise ProviderOperationError(
				f"Atlas cannot reach {self.server.ssh_host} through wg0. Check that {AtlasPeer.interface} holds this host.",
				is_retryable=True,
			) from error

	def save_progress(self) -> None:
		"""Store the current setup fields and commit the setup transaction."""
		self.server.db_set({field: self.server.get(field) for field in self.setup_fields})
		frappe.db.commit()  # nosemgrep
