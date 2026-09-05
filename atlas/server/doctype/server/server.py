# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections import deque
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.desk.utils import slug
from frappe.model.document import Document
from frappe.model.naming import make_autoname
from frappe.utils.background_jobs import is_job_enqueued
from frappe.utils.password import get_decrypted_password

from atlas.atlas.core.host_binaries import get_binary_download_url
from atlas.server.doctype.server_ssh_task.server_ssh_task import ServerSSHTask

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

WIREGUARD_OVERHEAD_BYTES = 60
WIREGUARD_CONFIGURE_TIMEOUT_SECONDS = 300
METALD_INSTALL_TIMEOUT_SECONDS = 1_200


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

	def before_validate(self) -> None:
		if not self.provider_server_id:
			self.settings.server_provider_controller.create_server(self)

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
		self.settings.server_provider_controller.reboot_server(self)

	@frappe.whitelist(methods=["POST"])
	def poweroff_server(self) -> None:
		"""Stop the provider server."""
		self._validate_power_action()
		self.settings.server_provider_controller.poweroff_server(self)
		self.db_set("status", "Stopped")

	@frappe.whitelist(methods=["POST"])
	def poweron_server(self) -> None:
		"""Start the provider server."""
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
			frappe.throw(_("Server setup is still running for {0}.").format(self.name))

		self.settings.server_provider_controller.archive_server(self)
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

		result = ServerSSHTask.create_for_command(
			server=self.name,
			command="lsblk --json --bytes --paths --output NAME,UUID,SIZE,MOUNTPOINT",
			run_in_background=False,
		).result
		if not result or not result.is_success:
			frappe.throw(_("Could not read the disks of server {0}.").format(self.name))

		self.set("disks", self._parse_disks(result.output))
		self.save()

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
		if not self.private_ipv4_address:
			frappe.throw(_("Server {0} needs a private IPv4 address.").format(self.name))
		if not self.private_network_interface:
			frappe.throw(_("Server {0} needs a private network interface.").format(self.name))

		if not self.settings.metald_binary_x86_64_file or not self.settings.wg_mesh_binary_x86_64_file:
			frappe.throw(_("Atlas Settings needs the metald and Atlas WG Mesh binaries."))

		token = get_decrypted_password("Server", self.name, "metald_api_token", raise_exception=False)
		if not token:
			token = frappe.generate_hash(length=128)
			self.metald_api_token = token
			self.save(ignore_permissions=True, ignore_version=True)

		token_hash = hashlib.sha256(token.encode()).hexdigest()
		result = ServerSSHTask.create_for_script_file(
			server=self.name,
			script_path="install-metald.sh",
			environment={
				"METALD_DOWNLOAD_URL": get_binary_download_url(self.settings.metald_binary_x86_64_file),
				"WG_MESH_DOWNLOAD_URL": get_binary_download_url(self.settings.wg_mesh_binary_x86_64_file),
				"METALD_AUTH_TOKEN_HASH": token_hash,
				"LISTEN_ADDRESS": "0.0.0.0:9000",
				"STORAGE_POOL_DEVICE": self.settings.server_provider_controller.get_storage_pool_device(self),
				# Atlas WG Mesh discovery runs on the private network, so its hook
				# belongs on that interface and never on the public uplink.
				"MESH_UPLINK_INTERFACE": self.private_network_interface,
			},
			timeout_seconds=METALD_INSTALL_TIMEOUT_SECONDS,
			run_in_background=False,
		).result
		if not result or not result.is_success:
			frappe.throw(_("Could not install metald on server {0}.").format(self.name))

	def _configure_wireguard(self) -> None:
		"""Configure WireGuard and store its public key."""
		self._set_wireguard_ip_address_if_not_set()
		result = ServerSSHTask.create_for_script_file(
			server=self.name,
			script_path="configure-wireguard.sh",
			environment={
				"WIREGUARD_ADDRESS": self.wireguard_ip_address,
				"WIREGUARD_LISTEN_PORT": self.port,
				"WIREGUARD_MTU": self.settings.private_network_mtu - WIREGUARD_OVERHEAD_BYTES,
			},
			timeout_seconds=WIREGUARD_CONFIGURE_TIMEOUT_SECONDS,
			run_in_background=False,
		).result
		if not result or not result.is_success:
			frappe.throw(_("Could not configure WireGuard on server {0}.").format(self.name))

		after_start = result.output.partition("===PUBLIC_KEY_START===")[2]
		public_key = after_start.partition("===PUBLIC_KEY_END===")[0].strip()
		if not public_key:
			frappe.throw(_("Server {0} reported no WireGuard public key.").format(self.name))

		self.db_set("wireguard_public_key", public_key)

	def _get_wireguard_ip_address(self) -> str:
		"""Return this server's fdab::/16 host mesh address."""
		node_number = self.name.rsplit("-", 1)[-1]
		if not node_number.isdigit():
			frappe.throw(_("Server {0} has no node number in its name.").format(self.name))

		region_id = self.settings.region_id
		if not 0 <= region_id <= 0xFFFF:
			frappe.throw(_("Atlas Settings region ID must fit in one IPv6 field."))

		return str(ipaddress.IPv6Address((0xFDAB << 112) | (region_id << 96) | int(node_number)))

	def _set_wireguard_ip_address_if_not_set(self) -> None:
		"""Set the WireGuard IP address if it is not already set."""
		if not self.wireguard_ip_address:
			self.db_set("wireguard_ip_address", self._get_wireguard_ip_address())

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
		storage_pool_device = self.settings.server_provider_controller.get_storage_pool_device(self)
		devices = deque(json.loads(lsblk_output).get("blockdevices", []))
		disks: dict[str, dict[str, str]] = {}
		while devices:
			device = devices.popleft()
			devices.extendleft(reversed(device.get("children") or []))
			name = device["name"]
			if not device.get("mountpoint") and name != storage_pool_device:
				continue

			disks[name] = {
				"device": name,
				"uuid": device.get("uuid") or "",
				"mount_point": device.get("mountpoint") or "",
				"size_gb": f"{int(device.get('size') or 0) / 1024**3:.2f}",
			}
		return list(disks.values())

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
