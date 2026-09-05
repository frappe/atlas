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

from atlas.atlas.core.server_providers.base import ServerCreateRequest, ServerPowerAction
from atlas.server.core.disk_inventory import DiskInventory
from atlas.server.core.host_installation import (
	METALD_INSTALL_TIMEOUT_SECONDS,
	WIREGUARD_CONFIGURE_TIMEOUT_SECONDS,
	HostInstallation,
)
from atlas.server.core.provisioning import ServerProvisioner
from atlas.server.doctype.server_ssh_task.server_ssh_task import ServerSSHTask

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


class Server(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.server.doctype.server_disk.server_disk import ServerDisk

		architecture: DF.Literal["amd64", "arm64"]
		disks: DF.Table[ServerDisk]
		is_provisioning_completed: DF.Check
		metald_api_token: DF.Password | None
		port: DF.Int
		private_ipv4_address: DF.Data | None
		private_network_interface: DF.Data | None
		provider_metadata: DF.Code | None
		provider_server_id: DF.Data | None
		public_ipv4_address: DF.Data | None
		public_network_interface: DF.Data | None
		server_image: DF.Link
		server_size: DF.Link
		status: DF.Literal["Pending", "Installing", "Running", "Stopped", "Failed", "Deleted"]
		wireguard_ip_address: DF.Data | None
		wireguard_public_key: DF.Data | None
	# end: auto-generated types

	@property
	def settings(self) -> AtlasSettings:
		if not hasattr(self, "_settings"):
			self._settings = frappe.get_single("Atlas Settings")

		return self._settings

	def autoname(self) -> None:
		if not self.settings.region_name:
			frappe.throw(_("Atlas Settings requires a region name before creating a Server"))

		self.name = make_autoname(f"node-{slug(self.settings.region_name)}-.#####", doc=self)

	def insert(self, *args: object, **kwargs: object) -> "Server":
		"""Insert this Server and remove only a new provider server after failure."""
		try:
			return super().insert(*args, **kwargs)
		except Exception:
			self._cleanup_provider_server_after_failed_insert()
			raise

	def before_validate(self) -> None:
		self.settings.server_provider_controller.validate_settings()
		self._validate_provider_catalog()
		if self.provider_server_id:
			return

		size = frappe.get_doc("Server Size", self.server_size)
		image = frappe.get_doc("Server Image", self.server_image)
		request = ServerCreateRequest(
			name=self.name,
			server_size=self.server_size,
			server_image=self.server_image,
			size_provider_metadata=self._provider_metadata(size.provider_metadata),
			image_provider_metadata=self._provider_metadata(image.provider_metadata),
		)
		provider_server = self.settings.server_provider_controller.ensure_server(request)
		self.provider_server_id = provider_server.provider_server_id
		if provider_server.status:
			self.status = provider_server.status
		self.public_ipv4_address = provider_server.public_ipv4_address
		self.provider_metadata = frappe.as_json(provider_server.provider_metadata)
		self.flags.provider_server_created = provider_server.was_created

	def validate(self) -> None:
		self._validate_provider_catalog()
		self._sync_disks_if_running()
		self._set_wireguard_ip_address_if_not_set()

	def after_insert(self) -> None:
		self._enqueue_setup_server()

	@property
	def setup_job_id(self) -> str:
		return f"atlas||server-provision||{self.name}"

	@frappe.whitelist(methods=["POST"])
	def ping_server(self) -> str:
		"""Create an SSH task that checks the running server."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Server {0} is not running.").format(self.name))

		ssh_task_name = ServerSSHTask.create_for_script_file(
			server=self.name, script_path="ping-server.sh"
		).name
		frappe.msgprint(
			_("Check the ping status <a href='/app/server-ssh-task/{0}'>here</a>").format(ssh_task_name)
		)
		return ssh_task_name

	@frappe.whitelist(methods=["POST"])
	def setup_server(self) -> None:
		"""Queue server setup again after a provisioning failure."""
		frappe.only_for("System Manager")
		if self.is_provisioning_completed:
			return

		if is_job_enqueued(self.setup_job_id):
			frappe.throw(_("Server setup is already running for {0}.").format(self.name))

		self.db_set("status", "Pending")
		self._enqueue_setup_server()

	@frappe.whitelist(methods=["POST"])
	def configure_wireguard(self) -> None:
		"""Queue the WireGuard setup for this server."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Server {0} is not running.").format(self.name))

		job_id = f"atlas||server||configure-wireguard||{self.name}"

		if is_job_enqueued(job_id):
			frappe.throw(_("WireGuard setup is already running for {0}.").format(self.name))

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_configure_wireguard",
			queue="long",
			timeout=WIREGUARD_CONFIGURE_TIMEOUT_SECONDS,
			job_id=job_id,
			deduplicate=True,
			enqueue_after_commit=True,
		)

	@frappe.whitelist(methods=["POST"])
	def reboot_server(self) -> None:
		"""Reboot the provider server."""
		self._validate_power_action()
		self.settings.server_provider_controller.set_power_state(
			self._provider_server_id(), ServerPowerAction.REBOOT
		)

	@frappe.whitelist(methods=["POST"])
	def poweroff_server(self) -> None:
		"""Stop the provider server."""
		self._validate_power_action()
		self.settings.server_provider_controller.set_power_state(
			self._provider_server_id(), ServerPowerAction.STOP
		)
		self.db_set("status", "Stopped")

	@frappe.whitelist(methods=["POST"])
	def poweron_server(self) -> None:
		"""Start the provider server."""
		self._validate_power_action()
		self.settings.server_provider_controller.set_power_state(
			self._provider_server_id(), ServerPowerAction.START
		)
		if self.is_provisioning_completed:
			self.db_set("status", "Running")

	@frappe.whitelist(methods=["POST"])
	def archive_server(self) -> None:
		"""Delete the provider server and mark this Server as deleted."""
		frappe.only_for("System Manager")
		if self.status == "Deleted":
			return

		if is_job_enqueued(self.setup_job_id):
			frappe.throw(_("Server setup is still running for {0}.").format(self.name))

		self.settings.server_provider_controller.delete_server(self._provider_server_id())
		self.db_set({"status": "Deleted", "is_provisioning_completed": 0})

	def _enqueue_setup_server(self) -> None:
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_setup_server",
			queue="long",
			timeout=7200,
			job_id=self.setup_job_id,
			deduplicate=True,
			enqueue_after_commit=True,
		)

	@frappe.whitelist(methods=["POST"])
	def sync_disks(self) -> None:
		"""Read the block devices on the server and replace the disks table."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Server {0} is not running.").format(self.name))

		DiskInventory(self).sync()

	@frappe.whitelist(methods=["POST"])
	def install_metald(self) -> None:
		"""Queue the metald setup for this server."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Server {0} is not running.").format(self.name))

		job_id = f"atlas||server||install-metald||{self.name}"

		if is_job_enqueued(job_id):
			frappe.throw(_("Metald setup already runs for {0}.").format(self.name))

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_install_metald",
			queue="long",
			timeout=METALD_INSTALL_TIMEOUT_SECONDS,
			job_id=job_id,
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
		server.insert()
		return server

	# Internal methods

	def _setup_server(self) -> None:
		ServerProvisioner(self).run()

	@staticmethod
	def _find_default_server_size(provider_type: str) -> str:
		sizes = frappe.get_all(
			"Server Size",
			filters={
				"enabled": 1,
				"provider_type": provider_type,
				"cpu_count": [">", 2],
				"memory_mib": [">=", 32768],
			},
			fields=["name"],
			order_by="memory_mib asc, cpu_count asc, disk_gib asc",
			limit=1,
		)
		if not sizes:
			frappe.throw(_("No enabled Server Size has more than 2 CPUs and at least 32 GiB of memory"))
		return sizes[0].name

	def _install_metald(self) -> None:
		"""Install metald and its host dependencies."""
		HostInstallation(self).install_metal()

	def _configure_wireguard(self) -> None:
		"""Configure WireGuard and store its public key."""
		HostInstallation(self).configure_wireguard()

	def _get_wireguard_ip_address(self) -> str:
		"""Return this server's fdab::/16 host mesh address."""
		return HostInstallation(self).wireguard_ip_address

	def _set_wireguard_ip_address_if_not_set(self) -> None:
		"""Set the WireGuard IP address if it is not already set."""
		HostInstallation(self).set_wireguard_ip_address()

	def _sync_disks_if_running(self) -> None:
		"""Fill the disks table once the server runs."""
		if self.status != "Running" or self.disks:
			return

		try:
			self.sync_disks()
		except Exception:
			frappe.log_error(title=f"Could not sync the disks of server {self.name}")

	def _parse_disks(self, lsblk_output: str) -> list[dict[str, str]]:
		"""Return one row for each mounted device and the raw storage pool device."""
		return DiskInventory(self).parse(lsblk_output)

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

	def _provider_server_id(self) -> str:
		"""Return the provider server ID for a remote operation."""
		if not self.provider_server_id:
			frappe.throw(_("Server {0} has no provider server ID.").format(self.name))
		return self.provider_server_id

	def _cleanup_provider_server_after_failed_insert(self) -> None:
		"""Delete the provider server that this insert request created."""
		if not getattr(self.flags, "provider_server_created", False) or not self.provider_server_id:
			return
		try:
			self.settings.server_provider_controller.delete_server(self.provider_server_id)
			self.flags.provider_server_created = False
		except Exception:
			frappe.log_error(title=f"Could not clean up server {self.name}")

	@staticmethod
	def _provider_metadata(value: str | None) -> dict:
		"""Return provider metadata as an object."""
		metadata = frappe.parse_json(value or "{}")
		if not isinstance(metadata, dict):
			frappe.throw(_("Provider metadata must be a JSON object."))
		return metadata
