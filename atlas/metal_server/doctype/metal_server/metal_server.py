# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, cint, now_datetime
from frappe.utils.background_jobs import is_job_enqueued

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.server_providers.base import ServerCreateRequest, ServerPowerAction
from atlas.atlas.core.tls.metal import CERTIFICATE_RENEWAL_WINDOW_DAYS, is_certificate_authority_expiring
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask
from atlas.metal_server.core.disk_inventory import DiskInventory
from atlas.metal_server.core.host_inspection import HostInspection
from atlas.metal_server.core.host_installation import (
	METALD_INSTALL_TIMEOUT_SECONDS,
	WIREGUARD_CONFIGURE_TIMEOUT_SECONDS,
	HostInstallation,
)
from atlas.metal_server.core.provisioning import ServerProvisioner
from atlas.metal_server.usage import enqueue_server_sync

if TYPE_CHECKING:
	from atlas.atlas.core.server_providers.aws.volumes import AwsVolumes
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


class MetalServer(Document):
	"""One Metal host, with its provider resources and provisioning state."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.metal_server.doctype.metal_server_disk.metal_server_disk import MetalServerDisk

		architecture: DF.Literal["amd64", "arm64"]
		disks: DF.Table[MetalServerDisk]
		is_provisioning_completed: DF.Check
		is_sleepy_vm_host: DF.Check
		metald_tls_certificate: DF.Password | None
		metald_tls_expires_on: DF.Datetime | None
		metald_tls_private_key: DF.Password | None
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
		title: DF.Data
		wireguard_ip_address: DF.Data | None
		wireguard_public_key: DF.Data | None
	# end: auto-generated types

	@property
	def ssh_host(self) -> str:
		"""Return the address an SSH Task connects to."""
		if not self.public_ipv4_address:
			frappe.throw(_("Metal Server {0} has no public IPv4 address.").format(self.name))

		return self.public_ipv4_address

	@property
	def settings(self) -> AtlasSettings:
		"""Return the Atlas settings this server uses."""
		if not hasattr(self, "_settings"):
			self._settings = frappe.get_single("Atlas Settings")

		return self._settings

	def before_validate(self) -> None:
		"""Fill values that depend on the selected size and image."""
		provider = self.settings.server_provider_controller
		provider.validate_settings()
		provider.validate_server(self)
		if self.provider_server_id:
			return

		size = frappe.get_doc("Metal Server Size", self.server_size)
		self.architecture = size.architecture

	def ensure_provider_server(self) -> None:
		"""Create the provider host once, including after a worker retry."""
		if self.provider_server_id:
			return

		size = frappe.get_doc("Metal Server Size", self.server_size)
		image = frappe.get_doc("Metal Server Image", self.server_image)
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

	def validate(self) -> None:
		"""Fill the mesh address."""
		self._set_wireguard_ip_address_if_not_set()

	def after_insert(self) -> None:
		"""Start provisioning in the background."""
		self._enqueue_setup_server()

	@property
	def setup_job_id(self) -> str:
		"""Return the background job identifier for this server."""
		return f"atlas||server-provision||{self.name}"

	@frappe.whitelist(methods=["POST"])
	def ping_server(self) -> str:
		"""Create an SSH task that checks the running server."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Metal Server {0} is not running.").format(self.name))

		ssh_task_name = SSHTask.create_for_script_file(
			target_type=self.doctype, target=self.name, script_path="ping-server.sh"
		).name
		frappe.msgprint(_("Check the ping status <a href='/app/ssh-task/{0}'>here</a>").format(ssh_task_name))
		return ssh_task_name

	@frappe.whitelist(methods=["POST"])
	def setup_server(self) -> None:
		"""Queue server setup again after a provisioning failure."""
		frappe.only_for("System Manager")
		if self.is_provisioning_completed:
			return

		if is_job_enqueued(self.setup_job_id):
			frappe.throw(_("Metal Server setup is already running for {0}.").format(self.name))

		self.db_set("status", "Pending")
		self._enqueue_setup_server()

	@frappe.whitelist(methods=["POST"])
	def configure_wireguard(self) -> None:
		"""Queue the WireGuard setup for this server."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Metal Server {0} is not running.").format(self.name))

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
			frappe.throw(_("Metal Server setup is still running for {0}.").format(self.name))

		if frappe.db.exists("Virtual Machine", {"server": self.name}):
			frappe.throw(_("Delete or migrate every Virtual Machine on {0} first.").format(self.name))

		self.settings.server_provider_controller.delete_server(
			self._provider_server_id(), self._provider_metadata(self.provider_metadata)
		)
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
		"""Queue a read of the block devices on the server."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Metal Server {0} is not running.").format(self.name))

		self.enqueue_disk_sync()

	def enqueue_disk_sync(self) -> None:
		"""Queue the replacement of the disks table from the host."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_sync_disks",
			job_id=f"atlas||server||sync-disks||{self.name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def _sync_disks(self) -> None:
		DiskInventory(self).sync()

	def onload(self) -> None:
		self.set_onload("server_provider", self.settings.server_provider)

	@frappe.whitelist(methods=["POST"])
	def get_volume(self, kind: str) -> dict:
		"""Return the current AWS values of the root or the storage volume."""
		frappe.only_for("System Manager")
		return self._aws_volumes(kind).describe(self, kind)

	@frappe.whitelist(methods=["POST"])
	def resize_volume(self, kind: str, size_gib: int, iops: int = 0, throughput_mibps: int = 0) -> None:
		"""Change one AWS volume and queue its growth on the host."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Metal Server {0} is not running.").format(self.name))
		self._aws_volumes(kind).modify(self, kind, cint(size_gib), cint(iops), cint(throughput_mibps))
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_grow_volume",
			kind=kind,
			queue="long",
			timeout=2_400,
			job_id=f"atlas||server||grow-volume||{kind}||{self.name}",
			deduplicate=True,
			enqueue_after_commit=False,
		)

	def _grow_volume(self, kind: str) -> None:
		self._aws_volumes(kind).grow(self, kind)

	def _aws_volumes(self, kind: str) -> "AwsVolumes":
		if self.settings.server_provider != "AWS" or kind not in ("root", "storage"):
			frappe.throw(_("Only an AWS root or storage volume can be resized."))
		return self.settings.server_provider_controller.volumes

	@frappe.whitelist(methods=["POST"])
	def sync_state(self) -> None:
		"""Queue one host state exchange for this server."""
		frappe.only_for("System Manager")
		if self.status != "Running" or not self.is_provisioning_completed:
			frappe.throw(_("Metal Server {0} is not ready to synchronize.").format(self.name))

		enqueue_server_sync(self.name)

	@property
	def metald_job_id(self) -> str:
		"""Return one job ID for each metald operation on this server."""
		return f"atlas||server||metald||{self.name}"

	@frappe.whitelist(methods=["POST"])
	def install_metald(self) -> None:
		"""Queue the metald setup for this server."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Metal Server {0} is not running.").format(self.name))

		job_id = self.metald_job_id

		if is_job_enqueued(job_id):
			frappe.throw(_("Another Metald operation already runs for {0}.").format(self.name))

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

	@frappe.whitelist(methods=["POST"])
	def renew_tls_certificate(self) -> None:
		"""Queue a Metal TLS certificate renewal for this server."""
		frappe.only_for("System Manager")
		if self.status != "Running" or not self.is_provisioning_completed:
			frappe.throw(_("Metal Server {0} is not ready to renew its certificate.").format(self.name))

		self.enqueue_tls_certificate_renewal()

	def enqueue_tls_certificate_renewal(self) -> None:
		"""Queue the renewal worker unless another Metald operation runs."""
		job_id = self.metald_job_id

		if is_job_enqueued(job_id):
			frappe.throw(_("Another Metald operation already runs for {0}.").format(self.name))

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_renew_tls_certificate",
			queue="long",
			timeout=METALD_INSTALL_TIMEOUT_SECONDS,
			job_id=job_id,
			deduplicate=True,
			enqueue_after_commit=True,
		)

	@frappe.whitelist(methods=["POST"])
	def upgrade_metald(self) -> None:
		"""Queue a metald binary upgrade for this server."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Metal Server {0} is not running.").format(self.name))

		job_id = self.metald_job_id

		if is_job_enqueued(job_id):
			frappe.throw(_("Another Metald operation already runs for {0}.").format(self.name))

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_upgrade_metald",
			queue="long",
			timeout=METALD_INSTALL_TIMEOUT_SECONDS,
			job_id=job_id,
			deduplicate=True,
			enqueue_after_commit=True,
		)

	# Static methods

	@staticmethod
	def provision(
		size: str,
		image: str,
		*,
		is_sleepy_vm_host: bool = False,
	) -> MetalServer:
		"""Insert a Pending Server with the selected image and size."""
		server: "MetalServer" = frappe.new_doc("Metal Server")
		server.server_size = size
		server.server_image = image
		server.is_sleepy_vm_host = is_sleepy_vm_host
		server.status = "Pending"
		server.insert(ignore_permissions=True)
		return server

	# Internal methods

	@run_as_admin
	def _setup_server(self) -> None:
		ServerProvisioner(self).run()

	def _install_metald(self) -> None:
		"""Install metald and its host dependencies."""
		HostInstallation(self).install_metal()

	def _renew_tls_certificate(self) -> None:
		"""Issue a current certificate and restart Metal with it."""
		HostInstallation(self).install_tls_credentials()

	def _upgrade_metald(self) -> None:
		"""Replace the metald binary and restart its daemon."""
		HostInstallation(self).upgrade_metald()

	def _configure_wireguard(self) -> None:
		"""Configure WireGuard and store its public key."""
		HostInstallation(self).configure_wireguard()

	def _get_wireguard_ip_address(self) -> str:
		"""Return this server's fdab::/16 host mesh address."""
		return HostInstallation(self).wireguard_ip_address

	def _set_wireguard_ip_address_if_not_set(self) -> None:
		"""Set the WireGuard IP address if it is not already set."""
		HostInstallation(self).set_wireguard_ip_address()

	def _parse_disks(self, lsblk_output: str) -> list[dict[str, str]]:
		"""Return one row for each mounted device and the raw storage pool device."""
		return DiskInventory(self).parse(lsblk_output)

	def _validate_power_action(self) -> None:
		"""Check that a power action can run for this Server."""
		frappe.only_for("System Manager")
		if self.status == "Deleted":
			frappe.throw(_("Metal Server {0} is deleted.").format(self.name))

		if is_job_enqueued(self.setup_job_id):
			frappe.throw(_("Metal Server setup still runs for {0}.").format(self.name))

	def _provider_server_id(self) -> str:
		"""Return the provider server ID for a remote operation."""
		if not self.provider_server_id:
			frappe.throw(_("Metal Server {0} has no provider server ID.").format(self.name))
		return self.provider_server_id

	@staticmethod
	def _provider_metadata(value: str | None) -> dict:
		"""Return provider metadata as an object."""
		metadata = frappe.parse_json(value or "{}")
		if not isinstance(metadata, dict):
			frappe.throw(_("Provider metadata must be a JSON object."))
		return metadata


def renew_expiring_tls_certificates() -> None:
	"""Queue a renewal for every ready host whose certificate expires soon."""
	if is_certificate_authority_expiring():
		frappe.log_error(title="The Metal certificate authority expires soon")

	servers = frappe.get_all(
		"Metal Server",
		filters={
			"status": "Running",
			"is_provisioning_completed": 1,
			"metald_tls_expires_on": ["<=", add_days(now_datetime(), CERTIFICATE_RENEWAL_WINDOW_DAYS)],
		},
		pluck="name",
	)
	for name in servers:
		server: MetalServer = frappe.get_doc("Metal Server", name)
		if is_job_enqueued(server.metald_job_id):
			continue
		server.enqueue_tls_certificate_renewal()


@frappe.whitelist(methods=["POST"])
def inspect_host(public_ipv4_address: str, private_ipv4_address: str) -> str:
	"""Queue a read-only inspection of an operator-prepared host."""
	frappe.only_for("System Manager")
	return HostInspection.start(public_ipv4_address, private_ipv4_address)


@frappe.whitelist()
def get_host_inspection(inspection_id: str) -> dict:
	"""Return the state of one host inspection."""
	frappe.only_for("System Manager")
	return HostInspection(inspection_id).state


@frappe.whitelist(methods=["POST"])
def create_host_disk_image(inspection_id: str, size_gib: int) -> None:
	"""Queue the creation of the disk image that holds the storage pool."""
	frappe.only_for("System Manager")
	HostInspection(inspection_id).start_disk_image_creation(int(size_gib))


@frappe.whitelist(methods=["POST"])
def register_host(inspection_id: str, storage_pool_device: str, provider_server_id: str | None = None) -> str:
	"""Create the Metal Server for an inspected host and return its name."""
	frappe.only_for("System Manager")
	return HostInspection(inspection_id).register(storage_pool_device, provider_server_id).name


def on_doctype_update() -> None:
	"""Index the ready host pool that placement filters on."""
	frappe.db.add_index(
		"Metal Server",
		["status", "is_provisioning_completed", "architecture", "is_sleepy_vm_host"],
		"placement_ready_pool",
	)
