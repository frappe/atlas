# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.core.mesh_address import get_region_mesh_address_prefix

if TYPE_CHECKING:
	from frappe.types import DF

MAX_PROXY_SERVERS = 5
ANYWHERE = ["0.0.0.0/0", "::/0"]


def get_proxy_firewall(region_id: int) -> dict[str, Any]:
	"""Admit HTTP and HTTPS from anywhere, and everything from regional services. Atlas reaches SSH through the host."""
	return {
		"enabled": True,
		"inbound": [
			{"protocol": "any", "cidrs": [f"{get_region_mesh_address_prefix(region_id)}::/64"]},
			{"protocol": "icmp", "cidrs": ANYWHERE},
			{"protocol": "tcp", "ports": "80", "cidrs": ANYWHERE},
			{"protocol": "tcp", "ports": "443", "cidrs": ANYWHERE},
		],
		"outbound": [{"protocol": "any", "cidrs": ANYWHERE}],
	}


class ProxyServer(Document):
	"""One regional HTTP proxy and its virtual machine."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		dns_health_check_id: DF.Data | None
		failure_message: DF.SmallText | None
		installed_package_hash: DF.Data | None
		is_provisioning_completed: DF.Check
		pushed_config_hash: DF.Data | None
		status: DF.Literal["Pending", "Provisioning", "Active", "Failed", "Archived"]
		tls_expires_on: DF.Datetime | None
		virtual_machine: DF.Link | None
	# end: auto-generated types

	@property
	def public_ipv4(self) -> str | None:
		"""Return the public IPv4 address of the proxy virtual machine."""
		if not self.virtual_machine:
			return None

		return frappe.get_doc("Virtual Machine", self.virtual_machine).public_ipv4

	def before_insert(self) -> None:
		"""Reject records created outside the Proxy Server API."""
		if not getattr(self.flags, "created_by_proxy_server_api", False):
			frappe.throw(_("Create Proxy Servers from the Proxy Server list."))

		active_count = frappe.db.count("Proxy Server", filters={"status": ["!=", "Archived"]})
		if active_count >= MAX_PROXY_SERVERS:
			frappe.throw(_("A region supports at most {0} proxy servers.").format(MAX_PROXY_SERVERS))

	@staticmethod
	def create(request: str | dict[str, Any]) -> dict[str, str | bool]:
		"""Create a Proxy Server and its virtual machine."""
		_validate_system_manager()
		values = frappe.parse_json(request) if isinstance(request, str) else request
		if not isinstance(values, dict):
			frappe.throw(_("Proxy Server creation data must be an object."))
		public_ipv4 = values.get("public_ipv4")
		if not isinstance(public_ipv4, str) or not public_ipv4.strip():
			frappe.throw(_("Select a reserved public IPv4 allocation."))

		proxy_server = frappe.new_doc("Proxy Server")
		proxy_server.flags.created_by_proxy_server_api = True
		proxy_server.flags.skip_initial_provisioning = True
		proxy_server.insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep

		is_draft = ProxyServer.create_virtual_machine(proxy_server, values)
		proxy_server.save(ignore_permissions=True)
		proxy_server.enqueue_provisioning()
		return {"name": proxy_server.name, "is_draft": is_draft}

	@staticmethod
	def create_virtual_machine(proxy_server: Any, values: dict[str, Any]) -> bool:
		"""Create the virtual machine and update the Proxy Server state."""
		from atlas.vm.core.vm_service import VirtualMachineCreateError, VirtualMachineService

		settings = frappe.get_single("Atlas Settings")
		virtual_machine_request = {
			"virtual_machine_image": values.get("virtual_machine_image"),
			"cpu_millicores": values.get("cpu_millicores"),
			"memory_mib": values.get("memory_mib"),
			"disk_mib": values.get("disk_mib"),
			"tenant_id": 0,
			"is_privileged": True,
			"is_termination_protected": True,
			"hostname": proxy_server.name,
			"ssh_keys": settings.public_ssh_key,
			"public_ipv4": values["public_ipv4"],
			"firewall": get_proxy_firewall(settings.region_id),
		}
		try:
			result = VirtualMachineService.create(virtual_machine_request)
			proxy_server.virtual_machine = result["name"]
			is_draft = bool(result["is_draft"])
		except VirtualMachineCreateError as error:
			proxy_server.virtual_machine = error.virtual_machine_name
			proxy_server.status = "Failed"
			proxy_server.failure_message = f"virtual-machine: {error}"
			is_draft = True

		return is_draft

	@frappe.whitelist()
	def get_domain(self) -> str:
		"""Return the name of this proxy below the Atlas wildcard domain."""
		wildcard_domain = frappe.get_cached_value("Atlas Settings", "Atlas Settings", "wildcard_domain")
		return f"{self.name}.{wildcard_domain}"

	def after_insert(self) -> None:
		"""Queue proxy setup after Frappe records the proxy."""
		if not getattr(self.flags, "skip_initial_provisioning", False):
			self.enqueue_provisioning()

	def on_trash(self) -> None:
		"""Refuse deletion while the virtual machine exists."""
		if self.status != "Archived" and self.virtual_machine:
			frappe.throw(_("Archive Proxy Server {0} before you delete it.").format(self.name))

	@frappe.whitelist(methods=["POST"])
	def provision(self) -> None:
		"""Queue the complete setup sequence again."""
		_validate_system_manager()
		if self.status == "Archived":
			frappe.throw(_("Proxy Server {0} is archived.").format(self.name))

		self.enqueue_provisioning()
		frappe.msgprint(_("Proxy Server setup has been queued. Please check after some time."))

	@frappe.whitelist(methods=["POST"])
	def update_dns_record(self) -> None:
		"""Point the proxy name at its current address."""
		_validate_system_manager()
		if self.status == "Archived":
			frappe.throw(_("Proxy Server {0} is archived.").format(self.name))

		from atlas.service.core.proxy.provisioning import ProxyServerProvisioner

		provisioner = ProxyServerProvisioner(self)
		provisioner.update_dns_record()
		if provisioner.proxy_server.status == "Archived":
			frappe.throw(_("Proxy Server {0} is archived.").format(self.name))

		frappe.msgprint(
			_("{0} now points at {1}.").format(
				provisioner.proxy_server.get_domain(), provisioner.proxy_server.public_ipv4
			)
		)

	@frappe.whitelist(methods=["POST"])
	def archive(self) -> None:
		"""Terminate the virtual machine, remove its DNS records, and detach it."""
		_validate_system_manager()
		if self.status == "Archived":
			return

		self.remove_dns_record()
		if self.virtual_machine:
			if frappe.db.exists("Virtual Machine", self.virtual_machine):
				virtual_machine = frappe.get_doc("Virtual Machine", self.virtual_machine)
				virtual_machine.set_termination_protection(False)
				virtual_machine.terminate()
			self.add_comment("Info", _("Archived with Virtual Machine {0}.").format(self.virtual_machine))

		self.db_set(
			{
				"status": "Archived",
				"is_provisioning_completed": 0,
				"dns_health_check_id": None,
				"virtual_machine": None,
			}
		)
		from atlas.service.core.proxy.configuration import push_configuration_to_active_proxies

		push_configuration_to_active_proxies()
		frappe.msgprint(_("Proxy Server {0} is archived.").format(self.name))

	def remove_dns_record(self) -> None:
		"""Remove proxy DNS records before its address is released."""
		settings = frappe.get_single("Atlas Settings")
		provider = settings.dns_provider_controller
		if self.dns_health_check_id:
			provider.remove_multivalue_a_record(
				f"proxy.{settings.wildcard_domain}",
				self.name,
			)
		if self.dns_health_check_id:
			provider.remove_health_check(self.dns_health_check_id)
			self.dns_health_check_id = None
		provider.remove_a_record(self.get_domain())

	def validate_is_reachable(self) -> None:
		"""Reject an action that needs a running virtual machine."""
		if not self.virtual_machine:
			frappe.throw(_("Proxy Server {0} has no virtual machine yet.").format(self.name))

	def enqueue_provisioning(self) -> None:
		"""Queue proxy setup."""
		enqueue_proxy_provisioning(self.name)

	def enqueue_configuration_push(self, enqueue_after_commit: bool = True) -> None:
		"""Queue configuration delivery to this proxy."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_push_configuration",
			queue="long",
			timeout=600,
			job_id=f"atlas||proxy-server||configure||{self.name}",
			deduplicate=True,
			enqueue_after_commit=enqueue_after_commit,
		)

	def _push_configuration(self) -> None:
		"""Push the current regional proxy configuration."""
		from atlas.service.core.proxy.provisioning import ProxyServerProvisioner

		ProxyServerProvisioner(self).push_configuration()

	def _provision(self) -> None:
		from atlas.service.core.proxy.provisioning import ProxyServerProvisioner

		ProxyServerProvisioner(self).run()


@frappe.whitelist(methods=["POST"])
def create(request: str | dict[str, Any]) -> dict[str, str | bool]:
	"""Call the Proxy Server creation service from the list view."""
	return ProxyServer.create(request)


def _validate_system_manager() -> None:
	frappe.only_for("System Manager")
	user_type = frappe.get_cached_value("User", frappe.session.user, "user_type")
	if user_type != "System User":
		frappe.throw(_("Only System Users can manage Proxy Servers."), frappe.PermissionError)


def enqueue_pending_proxies_provisioning() -> None:
	"""Queue setup for every pending Proxy Server."""
	for name in frappe.get_all("Proxy Server", filters={"status": "Pending"}, pluck="name"):
		enqueue_proxy_provisioning(name, enqueue_after_commit=False)


def enqueue_proxy_provisioning(proxy_server_name: str, enqueue_after_commit: bool = True) -> None:
	"""Queue setup for one Proxy Server."""
	frappe.enqueue_doc(
		"Proxy Server",
		proxy_server_name,
		"_provision",
		queue="long",
		timeout=3_600,
		job_id=f"atlas||proxy-server||provision||{proxy_server_name}",
		deduplicate=True,
		enqueue_after_commit=enqueue_after_commit,
	)
