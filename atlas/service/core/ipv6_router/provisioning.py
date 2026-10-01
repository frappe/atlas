from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import frappe
from frappe import _

from atlas.atlas.core.ssh import wait_for_server
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask
from atlas.service.core.service_package import IPV6_ROUTER_PACKAGE
from atlas.service.doctype.ipv6_router_server.ipv6_router_server import ipv6_router_lifecycle_lock
from atlas.vm.core.models import IPV6_INTERNET_DESTINATION, ROUTE_VIA_HOST, Route

if TYPE_CHECKING:
	from atlas.service.doctype.ipv6_router_server.ipv6_router_server import IPv6RouterServer
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine

INSTALL_TIMEOUT_SECONDS = 1_800
SSH_TIMEOUT_SECONDS = 600
SSH_POLL_INTERVAL_SECONDS = 5


class IPv6RouterServerProvisioner:
	"""Make the router VM a network gateway that owns the block, then install the router."""

	def __init__(self, router: IPv6RouterServer) -> None:
		self.router = router
		self.logger = logging.getLogger("atlas.service.provisioning")

	def run(self) -> None:
		"""Run each router setup step in order."""
		with ipv6_router_lifecycle_lock(self.router.name):
			self.router = frappe.get_doc("IPv6 Router Server", self.router.name)
			if self.router.status == "Failed" or not self.is_virtual_machine_ready:
				return

			phase = "start"
			self.router.status = "Provisioning"
			self.router.failure_message = None
			self.save()
			try:
				for phase, operation in self.steps:
					self.logger.info(
						"IPv6 router provisioning step started",
						extra={"resource": self.router.name, "operation": "provision", "phase": phase},
					)
					operation()

				self.router.status = "Active"
				self.save()
			except Exception as error:
				self.router.status = "Failed"
				self.router.failure_message = f"{phase}: {error}"
				self.save()
				frappe.db.commit()  # nosemgrep
				self.logger.exception(
					"IPv6 router provisioning failed",
					extra={"resource": self.router.name, "operation": "provision", "phase": phase},
				)
				frappe.log_error(title=f"IPv6 router provisioning failed during {phase}")
				raise

	@property
	def steps(self) -> tuple[tuple[str, Callable[[], None]], ...]:
		"""Return setup steps in execution order."""
		return (
			("network", self.configure_network),
			("secure-shell", self.wait_for_ssh),
			("installation", self.install_router),
		)

	@property
	def is_virtual_machine_ready(self) -> bool:
		"""Report whether the VM left the draft state and Metal holds it."""
		if not self.router.virtual_machine:
			return False

		virtual_machine = self.virtual_machine
		return not virtual_machine.is_draft and bool(virtual_machine.get_metal_vm_info())

	@property
	def virtual_machine(self) -> VirtualMachine:
		"""Return the router virtual machine."""
		return frappe.get_doc("Virtual Machine", self.router.virtual_machine)

	def configure_network(self) -> None:
		"""Give the VM the gateway role and the complete public pool."""
		from atlas.vm.core.vm_service import VirtualMachineService

		virtual_machine = self.virtual_machine
		information = virtual_machine.get_metal_vm_info()
		attached_prefix = information.desired.network.public_ipv6 if information else ""
		if attached_prefix and attached_prefix != self.router.prefix:
			frappe.throw(
				_("Router VM {0} holds IPv6 block {1}, not {2}.").format(
					virtual_machine.name, attached_prefix, self.router.prefix
				)
			)

		if not virtual_machine.is_network_gateway:
			virtual_machine.set_network_gateway(True)
		if not attached_prefix:
			pool = self.router.pool
			if not pool:
				frappe.throw(_("The router has no Public IP Pool."))
			pool.begin_provider_attach(virtual_machine.server)
			pool.reconcile()
			service = VirtualMachineService(virtual_machine)
			route = Route(IPV6_INTERNET_DESTINATION, ROUTE_VIA_HOST)
			service.update_network({"public_ipv6": pool.prefix, "routes": service.get_routes_with(route)})

	def wait_for_ssh(self) -> None:
		"""Wait for root SSH through the guest host."""
		wait_for_server(
			host=self.virtual_machine.ssh_host,
			users=("root",),
			timeout_seconds=SSH_TIMEOUT_SECONDS,
			poll_interval_seconds=SSH_POLL_INTERVAL_SECONDS,
			proxy_command=self.virtual_machine.get_ssh_proxy_command(),
		)

	def install_router(self) -> None:
		"""Install the router package with a synchronous SSH Task."""
		task = SSHTask.create_for_script_file(
			target_type="Virtual Machine",
			target=self.router.virtual_machine,
			script_path="install-service-package.sh",
			environment={
				**IPV6_ROUTER_PACKAGE.get_install_environment(),
				"REGION_ID": frappe.get_single("Atlas Settings").region_id,
				"PUBLIC_IPV6_PREFIX": self.router.prefix,
			},
			timeout_seconds=INSTALL_TIMEOUT_SECONDS,
			run_in_background=False,
		)
		self.router.installation_task = task.name
		self.save()

		result = task.result
		if result is None or not result.is_success:
			frappe.throw(_("IPv6 router installation failed. See SSH Task {0}.").format(task.name))

	def save(self) -> None:
		"""Store the current router state."""
		self.router.save(ignore_permissions=True)
