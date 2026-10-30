# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import make_autoname
from frappe.utils import add_days, cint, now_datetime
from frappe.utils.background_jobs import is_job_enqueued

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.server_providers.base import (
	ProviderOperationError,
	ServerCreateRequest,
	ServerPowerAction,
)
from atlas.atlas.core.tags import validate_tags
from atlas.atlas.core.telemetry.datum import get_datum_url
from atlas.atlas.core.tls.metal import CERTIFICATE_RENEWAL_WINDOW_DAYS, is_certificate_authority_expiring
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask
from atlas.metal_server.core.disk_inventory import DiskInventory
from atlas.metal_server.core.host_diagnostics import HostDiagnostics
from atlas.metal_server.core.host_inspection import HostInspection
from atlas.metal_server.core.host_installation import (
	FIREWALL_INSTALL_TIMEOUT_SECONDS,
	METALD_INSTALL_TIMEOUT_SECONDS,
	METALD_SETUP_TIMEOUT_SECONDS,
	TELEMETRY_INSTALL_TIMEOUT_SECONDS,
	WIREGUARD_CONFIGURE_TIMEOUT_SECONDS,
	HostInstallation,
	throw_script_failure,
)
from atlas.metal_server.core.provisioning import ServerProvisioner
from atlas.metal_server.usage import enqueue_server_sync
from atlas.operations.doctype.metal_server_drain.metal_server_drain import MetalServerDrain

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

# Warpgate uses the title as a target name and in `email:title` logins.
HOST_TITLE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class MetalServer(Document):
	"""One Metal host, with its provider resources and provisioning state."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.atlas.doctype.atlas_tag.atlas_tag import AtlasTag
		from atlas.metal_server.doctype.metal_server_disk.metal_server_disk import MetalServerDisk

		architecture: DF.Literal["amd64", "arm64"]
		bmc_password: DF.Password | None
		bmc_url: DF.Data | None
		bmc_username: DF.Data | None
		disks: DF.Table[MetalServerDisk]
		host_compatibility_report: DF.Code | None
		is_cpu_template_disabled: DF.Check
		is_in_maintenance: DF.Check
		is_provisioning_completed: DF.Check
		is_sleepy_vm_host: DF.Check
		metald_tls_certificate: DF.Password | None
		metald_tls_expires_on: DF.Datetime | None
		metald_tls_private_key: DF.Password | None
		port: DF.Int
		private_ipv4_address: DF.Data | None
		private_network_interface: DF.Data | None
		private_network_mac_address: DF.Data | None
		provider_metadata: DF.Code | None
		provider_server_id: DF.Data | None
		public_ipv4_address: DF.Data | None
		public_network_interface: DF.Data | None
		server_image: DF.Link
		server_size: DF.Link
		status: DF.Literal["Pending", "Installing", "Running", "Stopped", "Failed", "Deleted"]
		tags: DF.Table[AtlasTag]
		title: DF.Data | None
		tsc_scaling_supported: DF.Check
		wireguard_ip_address: DF.Data | None
		wireguard_public_key: DF.Data | None
	# end: auto-generated types

	@property
	def ssh_host(self) -> str:
		"""Return the wg0 address once the host holds the Atlas peer. Setup before that uses the public address."""
		if self.wireguard_public_key and self.wireguard_ip_address:
			return self.wireguard_ip_address

		if not self.public_ipv4_address:
			frappe.throw(_("Metal Server {0} has no public IPv4 address.").format(self.name))

		return self.public_ipv4_address

	@property
	def settings(self) -> AtlasSettings:
		"""Return the Atlas settings this server uses."""
		if not hasattr(self, "_settings"):
			self._settings = frappe.get_single("Atlas Settings")

		return self._settings

	@property
	def setup_job_id(self) -> str:
		"""Return the background job identifier for this server."""
		return f"atlas||server-provision||{self.name}"

	@property
	def metald_job_id(self) -> str:
		"""Return one job ID for each metald operation on this server."""
		return f"atlas||server||metald||{self.name}"

	@property
	def telemetry_job_id(self) -> str:
		"""Return one job ID for the telemetry setup of this server."""
		return f"atlas||server||telemetry||{self.name}"

	@property
	def bmc_power_state(self) -> str | None:
		"""Read BMC power state for Desk and expose failures in onload.

		Cache a good read for 10 seconds. A save changes `modified` and so starts a new cache entry.
		"""
		key = f"atlas:bmc_power_state:{self.name}:{self.modified}"
		if power_state := frappe.cache.get_value(key):
			return power_state

		try:
			power_state = self.settings.server_provider_controller.read_bmc_power_state(self)
		except ProviderOperationError as error:
			self.set_onload("bmc_power_error", str(error))
			return None

		if power_state:
			frappe.cache.set_value(key, power_state, expires_in_sec=10)
		return power_state

	def get_ssh_proxy_command(self) -> None:
		"""Return no proxy. Atlas connects to the host directly."""
		return None

	def before_validate(self) -> None:
		"""Fill values that depend on the selected size and image."""
		provider = self.settings.server_provider_controller
		provider.validate_settings()
		provider.validate_server(self)
		if self.provider_server_id:
			return

		size = frappe.get_doc("Metal Server Size", self.server_size)
		self.architecture = size.architecture

	def before_insert(self) -> None:
		self.title = make_autoname(f"metal-{self.settings.region_name}-.#", doc=self)

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
		"""Fill the mesh address and check the title and tags."""
		self._validate_title()
		validate_tags(self)
		self._validate_cpu_template()
		self._set_wireguard_ip_address_if_not_set()

	def _validate_cpu_template(self) -> None:
		"""Keep the CPU template disabled on a host that cannot run the built-in one."""
		report = frappe.parse_json((self.get_doc_before_save() or self).host_compatibility_report or "{}")
		if not self.is_cpu_template_disabled and (error := report.get("default_cpu_template_error")):
			frappe.throw(_("Metal Server {0} needs Disable CPU Template: {1}.").format(self.name, error))

	def before_save(self) -> None:
		"""Preserve synced host facts from the locked database row."""
		if previous := self.get_doc_before_save():
			self.host_compatibility_report = previous.host_compatibility_report
			self.tsc_scaling_supported = previous.tsc_scaling_supported

	def after_insert(self) -> None:
		"""Start provisioning in the background."""
		self._enqueue_setup_server()

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
	def fetch_metald_journal(self) -> dict:
		"""Read the last hour of the metald journal. Return the scrubbed output."""
		frappe.only_for("System Manager")
		return HostDiagnostics(self.name).fetch_metald_journal()

	@frappe.whitelist(methods=["POST"])
	def check_health(self) -> dict:
		"""Read the live state of the host. Return the scrubbed output."""
		frappe.only_for("System Manager")
		return HostDiagnostics(self.name).check_health()

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

	def onload(self) -> None:
		provider = self.settings.server_provider_controller
		self.set_onload("has_bmc_driver", provider.has_bmc_driver)
		self.set_onload("has_resizable_volumes", provider.has_resizable_volumes)
		self.set_onload(
			"running_drain",
			frappe.db.get_value(
				"Metal Server Drain", {"source_metal_server": self.name, "status": "Running"}
			),
		)

	@frappe.whitelist(methods=["POST"])
	def get_volume(self, kind: str) -> dict:
		"""Describe a root or storage volume."""
		frappe.only_for("System Manager")
		self._validate_volume_kind(kind)
		return self.settings.server_provider_controller.describe_volume(self, kind)

	@frappe.whitelist(methods=["POST"])
	def resize_volume(self, kind: str, size_gib: int, iops: int = 0, throughput_mibps: int = 0) -> None:
		"""Resize a provider volume and queue its growth on the host."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Metal Server {0} is not running.").format(self.name))
		self._validate_volume_kind(kind)
		self.settings.server_provider_controller.resize_volume(
			self, kind, cint(size_gib), cint(iops), cint(throughput_mibps)
		)
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

	@frappe.whitelist(methods=["POST"])
	def drain(
		self,
		destinations: list[str] | None = None,
		has_memory_snapshot: bool = False,
		allow_disk_only_fallback: bool = True,
		retry_failed_migrations: bool = True,
		parallel_migrations: int = 1,
	) -> str:
		"""Start moving every VM off this server, and return the new Metal Server Drain."""
		frappe.only_for("System Manager")
		drain = MetalServerDrain.start(
			self.name,
			destinations=destinations,
			has_memory_snapshot=cint(has_memory_snapshot),
			allow_disk_only_fallback=cint(allow_disk_only_fallback),
			retry_failed_migrations=cint(retry_failed_migrations),
			parallel_migrations=cint(parallel_migrations),
		)
		return drain.name

	@frappe.whitelist(methods=["POST"])
	def sync_state(self) -> None:
		"""Queue one host state exchange for this server."""
		frappe.only_for("System Manager")
		if self.status != "Running" or not self.is_provisioning_completed:
			frappe.throw(_("Metal Server {0} is not ready to synchronize.").format(self.name))

		enqueue_server_sync(self.name)

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
			timeout=METALD_SETUP_TIMEOUT_SECONDS,
			job_id=job_id,
			deduplicate=True,
			enqueue_after_commit=True,
		)

	@frappe.whitelist(methods=["POST"])
	def install_host_firewall(self) -> None:
		"""Queue the host firewall install for this server."""
		frappe.only_for("System Manager")
		if self.status != "Running":
			frappe.throw(_("Metal Server {0} is not running.").format(self.name))

		job_id = f"atlas||server||install-host-firewall||{self.name}"

		if is_job_enqueued(job_id):
			frappe.throw(_("The host firewall install already runs for {0}.").format(self.name))

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_install_host_firewall",
			queue="long",
			timeout=FIREWALL_INSTALL_TIMEOUT_SECONDS,
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
	def configure_telemetry(self) -> None:
		"""Queue Vector setup and a fresh Datum token for this server."""
		frappe.only_for("System Manager")
		if self.status != "Running" or not self.is_provisioning_completed:
			frappe.throw(_("Metal Server {0} is not ready for telemetry.").format(self.name))

		self.enqueue_telemetry_configuration()

	def enqueue_telemetry_configuration(self) -> None:
		"""Queue the telemetry setup. A queued setup absorbs a second request."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_configure_telemetry",
			queue="long",
			timeout=TELEMETRY_INSTALL_TIMEOUT_SECONDS,
			job_id=self.telemetry_job_id,
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

	@frappe.whitelist(methods=["POST"])
	def toggle_cpu_template(self) -> None:
		"""Flip Disable CPU Template and queue Upgrade Metald to apply it."""
		self.is_cpu_template_disabled = int(not self.is_cpu_template_disabled)
		self.save()
		self.upgrade_metald()

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

	@staticmethod
	def import_from_provider(provider_server_id: str, storage_pool_device: str | None = None) -> MetalServer:
		"""Insert a Metal Server for a provider server that Atlas did not create, or resume its setup."""
		if not provider_server_id:
			frappe.throw(_("Enter the provider server ID."))
		with frappe.db.advisory_lock(f"{frappe.db.cur_db_name}:host-import:{provider_server_id}"):
			frappe.db.rollback()  # nosemgrep
			name = frappe.db.get_value(
				"Metal Server", {"provider_server_id": provider_server_id, "status": ["!=", "Deleted"]}
			)
			if name:
				server: MetalServer = frappe.get_doc("Metal Server", name)
				if not server.is_provisioning_completed and not is_job_enqueued(server.setup_job_id):
					server._set_storage_pool_device(storage_pool_device)
					server.db_set({"provider_metadata": server.provider_metadata, "status": "Pending"})
					server._enqueue_setup_server()
			else:
				server = frappe.new_doc("Metal Server")
				server.provider_server_id = provider_server_id
				server._set_storage_pool_device(storage_pool_device)
				server.settings.server_provider_controller.import_server(server)
				server.architecture = frappe.db.get_value(
					"Metal Server Size", server.server_size, "architecture"
				)
				server.status = "Pending"
				server.insert(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
		return server

	def _validate_title(self) -> None:
		if not self.is_new() and not self.has_value_changed("title"):
			return

		if not HOST_TITLE.fullmatch(self.title or ""):
			frappe.throw(_("Title {0} is not one lowercase DNS label.").format(self.title))

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

	def _sync_disks(self) -> None:
		DiskInventory(self).sync()

	def _grow_volume(self, kind: str) -> None:
		self.settings.server_provider_controller.grow_volume(self, kind)

	def _validate_volume_kind(self, kind: str) -> None:
		if kind not in ("root", "storage"):
			frappe.throw(_("Only a root or storage volume can be resized."))

	@run_as_admin
	def _setup_server(self) -> None:
		ServerProvisioner(self).run()

	def _install_metald(self) -> None:
		"""Install metald and its host dependencies."""
		HostInstallation(self).install_metal()

	def _install_host_firewall(self) -> None:
		"""Install the host input firewall."""
		HostInstallation(self).install_host_firewall()

	def _configure_telemetry(self) -> None:
		"""Install Vector and the Datum credentials that metald and Vector read."""
		HostInstallation(self).install_telemetry()

	def _renew_tls_certificate(self) -> None:
		"""Issue a current certificate and restart Metal with it."""
		HostInstallation(self).install_tls_credentials()

	def _upgrade_metald(self) -> None:
		"""Replace metald and Atlas WG Mesh with the latest build and restart the daemon."""
		result = HostInstallation(self).upgrade_metald().result
		if not result or not result.is_success:
			throw_script_failure(_("Could not upgrade metald on server {0}.").format(self.name), result)

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

	def _set_storage_pool_device(self, storage_pool_device: str | None) -> None:
		"""Store the storage pool device that an import named in place of the provider device."""
		if storage_pool_device:
			metadata = self._provider_metadata(self.provider_metadata)
			self.provider_metadata = frappe.as_json({**metadata, "storage_pool_device": storage_pool_device})

	@staticmethod
	def _provider_metadata(value: str | None) -> dict:
		"""Return provider metadata as an object."""
		metadata = frappe.parse_json(value or "{}")
		if not isinstance(metadata, dict):
			frappe.throw(_("Provider metadata must be a JSON object."))
		return metadata


def renew_telemetry_credentials() -> None:
	"""Queue a telemetry setup with a fresh Datum token for every ready host. Runs daily."""
	if not get_datum_url():
		return

	servers = frappe.get_all(
		"Metal Server", filters={"status": "Running", "is_provisioning_completed": 1}, pluck="name"
	)
	for name in servers:
		server: MetalServer = frappe.get_doc("Metal Server", name)
		server.enqueue_telemetry_configuration()


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


@frappe.whitelist()
def get_server_creation_options() -> dict:
	"""Return host registration capabilities for the Metal Server list."""
	frappe.only_for("System Manager")
	provider = frappe.get_cached_doc("Atlas Settings").server_provider_controller
	return {
		"has_host_registration": provider.has_host_registration,
		"bmc_driver": provider.settings.bmc_driver if provider.has_bmc_driver else None,
	}


@frappe.whitelist(methods=["POST"])
def import_server(provider_server_id: str, storage_pool_device: str | None = None) -> str:
	"""Add a provider server that was created outside Atlas, and set it up."""
	frappe.only_for("System Manager")
	return MetalServer.import_from_provider(provider_server_id.strip(), storage_pool_device or None).name


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
def register_host(
	inspection_id: str,
	storage_pool_device: str,
	provider_server_id: str | None = None,
	bmc_url: str | None = None,
	bmc_username: str | None = None,
	bmc_password: str | None = None,
) -> str:
	"""Create the Metal Server for an inspected host and return its name."""
	frappe.only_for("System Manager")
	return (
		HostInspection(inspection_id)
		.register(
			storage_pool_device,
			provider_server_id,
			bmc_url=bmc_url,
			bmc_username=bmc_username,
			bmc_password=bmc_password,
		)
		.name
	)


def on_doctype_update() -> None:
	"""Index the ready host pool that placement filters on."""
	frappe.db.add_index(
		"Metal Server",
		["status", "is_provisioning_completed", "architecture", "is_sleepy_vm_host"],
		"placement_ready_pool",
	)
