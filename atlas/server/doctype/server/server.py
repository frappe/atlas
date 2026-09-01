# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.desk.utils import slug
from frappe.model.document import Document
from frappe.model.naming import make_autoname
from frappe.utils.background_jobs import is_job_enqueued

from atlas.server.doctype.server_ssh_task.server_ssh_task import ServerSSHTask

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


class Server(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		is_provisioning_completed: DF.Check
		private_ipv4_address: DF.Data | None
		private_network_interface: DF.Data | None
		provider_metadata: DF.Code | None
		provider_server_id: DF.Data | None
		public_ipv4_address: DF.Data | None
		public_network_interface: DF.Data | None
		server_image: DF.Link
		server_size: DF.Link
		status: DF.Literal["Pending", "Installing", "Running", "Stopped", "Failed", "Deleted"]
	# end: auto-generated types

	# A provider can take more than one hour to install the OS on a bare-metal server.
	setup_timeout_seconds = 7_200

	@property
	def settings(self) -> AtlasSettings:
		return frappe.get_single("Atlas Settings")

	def autoname(self) -> None:
		settings = frappe.get_single("Atlas Settings")
		if not settings.region_name:
			frappe.throw(_("Atlas Settings requires a region name before creating a Server"))

		self.name = make_autoname(f"node-{slug(settings.region_name)}-.#####", doc=self)

	def before_validate(self) -> None:
		self._validate_provider_catalog()
		if not self.provider_server_id:
			self.settings.server_provider_controller.create_server(self)

	def after_insert(self) -> None:
		self._enqueue_setup_server()

	@property
	def setup_job_id(self) -> str:
		return f"atlas||server-provision||{self.name}"

	@frappe.whitelist(methods=["POST"])
	def setup_server(self) -> None:
		"""Queue server setup again after a provisioning failure."""
		frappe.only_for("System Manager")
		if self.is_provisioning_completed:
			return

		if is_job_enqueued(self.setup_job_id):
			frappe.throw(_("Server setup already runs for {0}.").format(self.name))

		self.db_set("status", "Pending")
		self._enqueue_setup_server()

	@frappe.whitelist(methods=["POST"])
	def ping_server(self) -> str:
		"""Create an SSH task that checks the running server."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Server {0} is not running.").format(self.name))

		ssh_task_name = ServerSSHTask.create_for_script_file(
			server=self.name, script_path="ping-server.sh"
		).name
		frappe.msgprint(_(f"Check ping status <a href='/app/server-ssh-task/{ssh_task_name}'>here</a>"))

	@frappe.whitelist(methods=["POST"])
	def reboot_server(self) -> None:
		"""Reboot the provider server."""
		self._validate_power_action()
		self.settings.server_provider_controller.reboot_server(self)

	@frappe.whitelist(methods=["POST"])
	def poweroff_server(self) -> None:
		"""Stop the provider server."""
		self._validate_power_action()
		self.settings.server_provider_controller.poweroff_server(self)
		self.db_set("status", "Stopped")

	@frappe.whitelist(methods=["POST"])
	def poweron_server(self) -> None:
		"""Start the provider server.

		The status stays as it is while provisioning is not complete, because setup
		owns the status until it finishes.
		"""
		self._validate_power_action()
		self.settings.server_provider_controller.poweron_server(self)
		if self.is_provisioning_completed:
			self.db_set("status", "Running")

	@frappe.whitelist(methods=["POST"])
	def archive_server(self) -> None:
		"""Delete the provider server and mark this Server as deleted."""
		frappe.only_for("System Manager")
		if self.status == "Deleted":
			return

		if is_job_enqueued(self.setup_job_id):
			frappe.throw(_("Server setup still runs for {0}.").format(self.name))

		self.settings.server_provider_controller.archive_server(self)
		self.db_set({"status": "Deleted", "is_provisioning_completed": 0})

	def _enqueue_setup_server(self) -> None:
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_setup_server",
			queue="long",
			timeout=self.setup_timeout_seconds,
			job_id=self.setup_job_id,
			deduplicate=True,
			enqueue_after_commit=True,
		)

	# Static methods

	@staticmethod
	def provision(os_name: str = "Ubuntu", version: str = "26.04", size: str | None = None) -> "Server":
		"""Create and provision a Server with the selected image and size."""
		settings: AtlasSettings = frappe.get_single("Atlas Settings")
		image = frappe.get_doc("Server Image", f"{settings.server_provider}/{os_name}_{version}")

		server: "Server" = frappe.new_doc("Server")
		server.server_size = size or Server._find_default_server_size(settings.server_provider)
		server.server_image = image.name
		server.status = "Pending"
		provider = settings.server_provider_controller
		try:
			server.insert()
		except Exception:
			try:
				provider.cleanup_provisioned_server(server)
			except Exception:
				frappe.log_error(title=f"Could not clean up server {server.name}")
			raise
		return server

	# Internal methods

	def _setup_server(self) -> None:
		self.settings.server_provider_controller.run_provisioning(self)

	@staticmethod
	def _find_default_server_size(provider_type: str) -> str:
		sizes = frappe.get_all(
			"Server Size",
			filters={
				"enabled": 1,
				"provider_type": provider_type,
				"cpu_count": [">", 2],
				"memory_mb": [">=", 32768],
			},
			fields=["name"],
			order_by="memory_mb asc, cpu_count asc, disk_gib asc",
			limit=1,
		)
		if not sizes:
			frappe.throw(_("No enabled Server Size has more than 2 CPUs and at least 32 GiB of memory"))
		return sizes[0].name

	def _validate_provider_catalog(self) -> None:
		provider_type = self.settings.server_provider
		for doctype, name in (("Server Size", self.server_size), ("Server Image", self.server_image)):
			if name and frappe.db.get_value(doctype, name, "provider_type") != provider_type:
				frappe.throw(_("{0} must belong to the configured server provider").format(doctype))

	def _validate_power_action(self) -> None:
		"""Check that a power action can run for this Server."""
		frappe.only_for("System Manager")
		if self.status == "Deleted":
			frappe.throw(_("Server {0} is deleted.").format(self.name))

		if is_job_enqueued(self.setup_job_id):
			frappe.throw(_("Server setup still runs for {0}.").format(self.name))
