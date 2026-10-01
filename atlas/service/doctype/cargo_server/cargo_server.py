# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.ssh import SSHRunner
from atlas.service.core.cargo.storage_cluster import (
	remove_storage_cluster_config,
	store_storage_cluster_config,
)
from atlas.service.core.cargo.telemetry import remove_telemetry_config, store_telemetry_config

if TYPE_CHECKING:
	from collections.abc import Iterator

LIFECYCLE_LOCK_NAME = "atlas:cargo-server:lifecycle"
PROVISIONABLE_STATUSES = ("Not Provisioned", "Archived")
PILOT_ADMIN_PASSWORD_RESET_COMMAND = """set -euo pipefail
quoted_password=$(printf '%q' "$PILOT_ADMIN_PASSWORD")
su - frappe -c "pilot set-admin-password --password $quoted_password"
"""
PILOT_RELEASE_TRACKER_COMMAND = """set -euo pipefail
quoted_site=$(printf '%q' "$CARGO_SITE")
su - frappe -c "pilot -b cargo --site $quoted_site $PILOT_RELEASE_TRACKER_COMMAND"
"""


class CargoServer(Document):
	"""Own the regional Cargo service and its virtual machine."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		auto_build_pilot_images: DF.Check
		failure_message: DF.SmallText | None
		installation_task: DF.Link | None
		pilot_release_tracker_pending: DF.Check
		status: DF.Literal["Not Provisioned", "Pending", "Provisioning", "Active", "Failed", "Archived"]
		virtual_machine: DF.Link | None
	# end: auto-generated types

	@property
	def domain(self) -> str:
		"""Return the public Cargo domain."""
		return f"cargo.{self.wildcard_domain}"

	@property
	def pilot_domain(self) -> str:
		"""Return the domain of the Pilot administration panel on the Cargo host."""
		return f"cargo-pilot.{self.wildcard_domain}"

	@property
	def wildcard_domain(self) -> str:
		"""Return the regional wildcard domain."""
		return frappe.get_cached_value("Atlas Settings", "Atlas Settings", "wildcard_domain")

	@frappe.whitelist(methods=["POST"])
	def provision(self, request: str | dict[str, Any]) -> dict[str, str | bool]:
		"""Create and queue setup for the regional Cargo virtual machine."""
		_validate_system_manager()
		values = frappe.parse_json(request) if isinstance(request, str) else request
		if not isinstance(values, dict):
			frappe.throw(_("Cargo Server provisioning data must be an object."))

		with cargo_lifecycle_lock():
			cargo_server: CargoServer = frappe.get_single("Cargo Server")
			cargo_server._validate_provision_request(values)
			store_storage_cluster_config(values.get("storage_cluster"))
			store_telemetry_config(values.get("telemetry"))
			cargo_server.status = "Pending"
			cargo_server.failure_message = None
			cargo_server.installation_task = None
			cargo_server.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep

			is_draft = cargo_server._create_virtual_machine(values)
			cargo_server.save(ignore_permissions=True)
			if cargo_server.status != "Failed":
				cargo_server.enqueue_provisioning()

		if cargo_server.status == "Failed":
			frappe.msgprint(_("Cargo virtual machine creation failed. Archive it before you try again."))
		else:
			frappe.msgprint(_("Cargo Server setup has been queued. Please check after some time."))
		return {"name": cargo_server.name, "is_draft": is_draft}

	@frappe.whitelist(methods=["POST"])
	def reset_pilot_admin_password(self) -> dict[str, str]:
		"""Set a new Pilot administration password on the Cargo host and return it."""
		_validate_system_manager()
		with cargo_lifecycle_lock():
			cargo_server: CargoServer = frappe.get_single("Cargo Server")
			if cargo_server.status != "Active":
				frappe.throw(_("Cargo Server must be Active to reset the Pilot administration password."))

			from atlas.service.core.cargo.provisioning import generate_installer_password

			password = generate_installer_password()
			virtual_machine = frappe.get_doc("Virtual Machine", cargo_server.virtual_machine)
			result = SSHRunner(
				virtual_machine.ssh_host, proxy_command=virtual_machine.get_ssh_proxy_command()
			).run_command(
				PILOT_ADMIN_PASSWORD_RESET_COMMAND,
				data={"PILOT_ADMIN_PASSWORD": password},
			)
			if not result.is_success:
				frappe.throw(_("The Pilot administration password reset failed."))

		return {"password": password, "domain": cargo_server.pilot_domain}

	@frappe.whitelist(methods=["POST"])
	def enable_pilot_release_tracker(self) -> None:
		"""Enable image builds for new Pilot releases."""
		_validate_system_manager()
		with cargo_lifecycle_lock():
			cargo_server: CargoServer = frappe.get_single("Cargo Server")
			cargo_server._set_pilot_release_tracker(enabled=True)

	@frappe.whitelist(methods=["POST"])
	def disable_pilot_release_tracker(self) -> None:
		"""Disable image builds for new Pilot releases."""
		_validate_system_manager()
		with cargo_lifecycle_lock():
			cargo_server: CargoServer = frappe.get_single("Cargo Server")
			cargo_server._set_pilot_release_tracker(enabled=False)

	@frappe.whitelist(methods=["POST"])
	def archive(self) -> None:
		"""Remove the proxy routes and terminate the Cargo virtual machine."""
		_validate_system_manager()
		with cargo_lifecycle_lock():
			cargo_server = frappe.get_single("Cargo Server")
			if not cargo_server.virtual_machine:
				if cargo_server.status == "Archived":
					return
				frappe.throw(_("Cargo Server has no virtual machine to archive."))

			try:
				from atlas.service.core.cargo.provisioning import CargoServerProvisioner

				CargoServerProvisioner(cargo_server).remove_proxy_routes()
				if frappe.db.exists("Virtual Machine", cargo_server.virtual_machine):
					virtual_machine = frappe.get_doc("Virtual Machine", cargo_server.virtual_machine)
					virtual_machine.set_termination_protection(False)
					virtual_machine.terminate()
			except Exception as error:
				cargo_server.status = "Failed"
				cargo_server.failure_message = f"archive: {error}"
				cargo_server.save(ignore_permissions=True)
				frappe.db.commit()  # nosemgrep
				raise

			remove_storage_cluster_config()
			remove_telemetry_config()
			cargo_server.status = "Archived"
			cargo_server.failure_message = None
			cargo_server.virtual_machine = None
			cargo_server.installation_task = None
			cargo_server.save(ignore_permissions=True)

		frappe.msgprint(_("Cargo Server is archived."))

	def enqueue_provisioning(self, enqueue_after_commit: bool = True) -> None:
		"""Queue Cargo installation for the attached virtual machine."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_provision",
			queue="long",
			timeout=3_600,
			job_id="atlas||cargo-server||provision",
			deduplicate=True,
			enqueue_after_commit=enqueue_after_commit,
		)

	def _provision(self) -> None:
		from atlas.service.core.cargo.provisioning import CargoServerProvisioner

		CargoServerProvisioner(self).run()

	def _validate_provision_request(self, values: dict[str, Any]) -> None:
		"""Reject a provision request that cannot produce a working Cargo Server."""
		if self.virtual_machine:
			frappe.throw(_("Archive the current Cargo Server before you provision another one."))
		if self.status not in PROVISIONABLE_STATUSES:
			frappe.throw(_("Cargo Server cannot be provisioned while its status is {0}.").format(self.status))

		if not frappe.db.exists("Proxy Server", {"status": "Active"}):
			frappe.throw(_("Provision an Active Proxy Server before you provision Cargo Server."))

		image_name = values.get("virtual_machine_image")
		if (
			not image_name
			or frappe.db.get_value("Virtual Machine Image", image_name, "image_type") != "system"
		):
			frappe.throw(_("Select a System Virtual Machine Image."))

	def _create_virtual_machine(self, values: dict[str, Any]) -> bool:
		from atlas.vm.core.vm_service import VirtualMachineCreateError, VirtualMachineService

		request = {
			"virtual_machine_image": values.get("virtual_machine_image"),
			"cpu_millicores": values.get("cpu_millicores"),
			"memory_mib": values.get("memory_mib"),
			"disk_mib": values.get("disk_mib"),
			"tenant_id": 0,
			"is_privileged": True,
			"is_termination_protected": True,
			"hostname": "cargo",
			"ssh_keys": frappe.get_single("Atlas Settings").public_ssh_key,
		}
		try:
			result = VirtualMachineService.create(request)
			self.virtual_machine = result["name"]
			return bool(result["is_draft"])
		except VirtualMachineCreateError as error:
			self.virtual_machine = error.virtual_machine_name
			self.status = "Failed"
			self.failure_message = f"virtual-machine: {error}"
			return True

	def _set_pilot_release_tracker(self, enabled: bool) -> None:
		"""Set Cargo release tracking after its image storage is ready."""
		if self.status != "Active":
			frappe.throw(_("Cargo Server must be Active to change Pilot release tracking."))
		if enabled and not can_enable_pilot_release_tracker():
			frappe.throw(_("Wait for object storage and all available bootstrap image migrations."))

		virtual_machine = frappe.get_doc("Virtual Machine", self.virtual_machine)
		command = "enable-pilot-release-tracker" if enabled else "disable-pilot-release-tracker"
		result = SSHRunner(
			virtual_machine.ssh_host, proxy_command=virtual_machine.get_ssh_proxy_command()
		).run_command(
			PILOT_RELEASE_TRACKER_COMMAND,
			data={"CARGO_SITE": self.domain, "PILOT_RELEASE_TRACKER_COMMAND": command},
		)
		if not result.is_success:
			frappe.throw(_("Cargo could not change Pilot release tracking."))

		self.auto_build_pilot_images = enabled
		self.pilot_release_tracker_pending = False
		self.save(ignore_permissions=True)


@contextmanager
def cargo_lifecycle_lock() -> Iterator[None]:
	"""Serialize Cargo lifecycle actions. One Single owns one virtual machine."""
	try:
		with filelock(LIFECYCLE_LOCK_NAME, timeout=0):
			yield
	except LockTimeoutError:
		frappe.throw(_("Another Cargo Server lifecycle action is in progress."))


def _validate_system_manager() -> None:
	frappe.only_for("System Manager")
	user_type = frappe.get_cached_value("User", frappe.session.user, "user_type")
	if user_type != "System User":
		frappe.throw(_("Only System Users can manage Cargo Server."), frappe.PermissionError)


def has_site_file_images() -> bool:
	"""Return true while an available bootstrap image uses site file storage."""
	return frappe.db.exists("Virtual Machine Image", {"artifact_storage": "Site File", "status": "Available"})


def can_enable_pilot_release_tracker() -> bool:
	"""Return true when Cargo can build new images in object storage."""
	return frappe.get_single("Atlas Settings").is_object_storage_configured and not has_site_file_images()


def enqueue_pilot_release_tracker_enable() -> None:
	"""Queue release tracking when bootstrap image migration makes it possible."""
	frappe.enqueue(
		"atlas.service.doctype.cargo_server.cargo_server.enable_pilot_release_tracker_after_migration",
		queue="long",
		timeout=120,
		job_id="atlas||cargo-server||enable-pilot-release-tracker",
		deduplicate=True,
		enqueue_after_commit=False,
	)


def enqueue_pending_pilot_release_tracker_enable() -> None:
	"""Retry automatic release tracking while it remains pending."""
	cargo_server: CargoServer = frappe.get_single("Cargo Server")
	if cargo_server.pilot_release_tracker_pending:
		enqueue_pilot_release_tracker_enable()


@run_as_admin
def enable_pilot_release_tracker_after_migration() -> None:
	"""Enable Cargo release tracking after the last bootstrap image migration."""
	with cargo_lifecycle_lock():
		cargo_server: CargoServer = frappe.get_single("Cargo Server")
		if (
			not cargo_server.pilot_release_tracker_pending
			or cargo_server.status != "Active"
			or not can_enable_pilot_release_tracker()
		):
			return
		cargo_server._set_pilot_release_tracker(enabled=True)


def enqueue_pending_cargo_provisioning() -> None:
	"""Continue a pending Cargo setup after virtual machine reconciliation."""
	cargo_server = frappe.get_single("Cargo Server")
	if cargo_server.status == "Pending" and cargo_server.virtual_machine:
		cargo_server.enqueue_provisioning(enqueue_after_commit=False)
