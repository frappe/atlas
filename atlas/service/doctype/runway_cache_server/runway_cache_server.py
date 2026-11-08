from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.synchronization import filelock

from atlas.atlas.core.mesh_address import get_region_mesh_address_prefix


def validate_https_url(value: str) -> None:
	"""Require a public configuration URL without embedded credentials."""
	url = urlsplit(value or "")
	if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or url.query:
		frappe.throw(_("Use an HTTPS URL without credentials, a query, or a fragment."))


def get_cache_firewall(region_id: int, guest_port: int, operator_port: int) -> dict[str, Any]:
	"""Keep operator traffic in tenant zero; guests connect directly over the regional mesh."""
	prefix = get_region_mesh_address_prefix(region_id)
	return {
		"enabled": True,
		"inbound": [
			{"protocol": "tcp", "ports": str(guest_port), "cidrs": [f"{prefix}::/32"]},
			{"protocol": "tcp", "ports": str(operator_port), "cidrs": [f"{prefix}::/64"]},
		],
		"outbound": [{"protocol": "any", "cidrs": ["0.0.0.0/0", "::/0"]}],
	}


class RunwayCacheServer(Document):
	"""Own the region's single cache VM and its release pin."""

	@property
	def domain(self) -> str:
		return f"runway-cache-svc.{frappe.get_single('Atlas Settings').wildcard_domain}"

	@property
	def internal_domain(self) -> str:
		return f"internal-{self.domain}"

	def validate(self) -> None:
		if not self.release_url and not self.virtual_machine:
			return
		validate_https_url(self.release_url)
		validate_https_url(self.central_url)
		validate_https_url(self.central_jwks_url)
		if not re.fullmatch(r"[0-9a-f]{64}", self.release_sha256 or ""):
			frappe.throw(_("Release SHA256 must have 64 lowercase hexadecimal characters."))
		if not self.central_region or not self.central_issuer:
			frappe.throw(_("Set the Central region and issuer."))
		origin = urlsplit(self.central_url)
		if origin.path not in ("", "/"):
			frappe.throw(_("Central URL must be an HTTPS origin."))
		if self.central_jwks_url != self.central_url.rstrip("/") + "/api/method/central.api.jwks.get_jwks":
			frappe.throw(_("Use the Central origin's JWKS endpoint."))
		for port in (self.guest_port, self.operator_port):
			if not isinstance(port, int) or not 1024 <= port <= 65535:
				frappe.throw(_("Cache listener ports must be between 1024 and 65535."))
		if self.guest_port == self.operator_port:
			frappe.throw(_("Guest and operator listeners need different ports."))
		previous = self.get_doc_before_save()
		if previous and previous.virtual_machine:
			for field in ("guest_port", "operator_port", "central_region"):
				if self.get(field) != previous.get(field):
					frappe.throw(_("Archive the cache VM before changing its listener ports or region."))

	@frappe.whitelist(methods=["POST"])
	def provision(self, request: str | dict[str, Any]) -> dict[str, str | bool]:
		"""Create a privileged service VM through the normal VM lifecycle."""
		_validate_system_manager()
		values = frappe.parse_json(request) if isinstance(request, str) else request
		if not isinstance(values, dict):
			frappe.throw(_("Provisioning data must be an object."))
		from atlas.vm.core.models import VirtualMachineCreateRequest
		from atlas.vm.core.vm_service import VirtualMachineCreateError, VirtualMachineService

		with cache_lifecycle_lock():
			cache = frappe.get_single("Runway Cache Server")
			if cache.virtual_machine or cache.status not in ("Pending", "Archived"):
				frappe.throw(_("Archive the current cache VM before provisioning another."))
			cache.validate()
			if not cache.release_url or not frappe.db.exists("Proxy Server", {"status": "Active"}):
				frappe.throw(_("Set the release pin and provision an Active Proxy Server first."))
			settings = frappe.get_single("Atlas Settings")
			request = {
				key: values.get(key)
				for key in ("virtual_machine_image", "cpu_millicores", "memory_mib", "disk_mib")
			}
			request.update(
				tenant_id=0,
				is_privileged=True,
				is_termination_protected=True,
				hostname="runway-cache",
				ssh_keys=settings.public_ssh_key,
				firewall=get_cache_firewall(settings.region_id, cache.guest_port, cache.operator_port),
			)
			parsed = VirtualMachineCreateRequest.from_value(request)
			image = VirtualMachineService.get_image(parsed.virtual_machine_image, 0)
			if image.image_type != "system":
				frappe.throw(_("Select a System Virtual Machine Image."))
			image.validate_compatibility(parsed.disk_mib)
			cache.status = "Pending"
			cache.failure_message = None
			cache.operator_ca = None
			cache.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
			try:
				result = VirtualMachineService.create(request)
				cache.virtual_machine = result["name"]
				is_draft = bool(result["is_draft"])
			except VirtualMachineCreateError as error:
				cache.virtual_machine = error.virtual_machine_name
				cache.status = "Failed"
				cache.failure_message = f"virtual-machine: {error}"
				is_draft = True
			cache.save(ignore_permissions=True)
			if cache.status != "Failed":
				cache.enqueue_provisioning()
			return {"name": cache.name, "is_draft": is_draft}

	@frappe.whitelist(methods=["POST"])
	def reprovision(self) -> None:
		"""Retry setup or apply a changed release pin on the same disk."""
		_validate_system_manager()
		with cache_lifecycle_lock():
			cache = frappe.get_single("Runway Cache Server")
			if not cache.virtual_machine or cache.status == "Archived":
				frappe.throw(_("Provision a cache VM first."))
			cache.status = "Pending"
			cache.failure_message = None
			cache.save(ignore_permissions=True)
			cache.enqueue_provisioning()

	@frappe.whitelist(methods=["POST"])
	def archive(self) -> None:
		"""Remove the route and DNS before terminating the VM."""
		_validate_system_manager()
		from atlas.service.core.proxy.configuration import push_configuration_to_active_proxies
		from atlas.service.core.runway_cache.provisioning import RunwayCacheServerProvisioner

		with cache_lifecycle_lock():
			cache = frappe.get_single("Runway Cache Server")
			if cache.status == "Archived" and not cache.virtual_machine:
				return
			try:
				cache.status = "Archived"
				cache.failure_message = None
				cache.save(ignore_permissions=True)
				frappe.db.commit()  # nosemgrep
				RunwayCacheServerProvisioner(cache).push_proxy_routes()
				frappe.get_single("Atlas Settings").dns_provider_controller.remove_service_record(
					cache.internal_domain
				)
				if cache.virtual_machine:
					vm = frappe.get_doc("Virtual Machine", cache.virtual_machine)
					vm.set_termination_protection(False)
					vm.terminate()
			except Exception as error:
				cache.status = "Failed"
				cache.failure_message = f"archive: {error}"
				cache.save(ignore_permissions=True)
				frappe.db.commit()  # nosemgrep
				raise

			cache.virtual_machine = None
			cache.installation_task = None
			cache.operator_ca = None
			cache.save(ignore_permissions=True)
			push_configuration_to_active_proxies()

	def enqueue_provisioning(self, enqueue_after_commit: bool = True) -> None:
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_provision",
			queue="long",
			timeout=3600,
			job_id="atlas||runway-cache-server||provision",
			deduplicate=True,
			enqueue_after_commit=enqueue_after_commit,
		)

	def _provision(self) -> None:
		from atlas.service.core.runway_cache.provisioning import RunwayCacheServerProvisioner

		RunwayCacheServerProvisioner(self).run()


@contextmanager
def cache_lifecycle_lock() -> Iterator[None]:
	"""Serialize creation, setup, upgrades, and archive for the singleton."""
	with filelock("atlas:runway-cache-server:lifecycle", timeout=0):
		yield


def _validate_system_manager() -> None:
	frappe.only_for("System Manager")
	if frappe.get_cached_value("User", frappe.session.user, "user_type") != "System User":
		frappe.throw(_("Only System Users can manage Runway Cache Server."), frappe.PermissionError)


def enqueue_pending_cache_provisioning() -> None:
	cache = frappe.get_single("Runway Cache Server")
	if cache.status in ("Pending", "Provisioning") and cache.virtual_machine:
		cache.enqueue_provisioning(enqueue_after_commit=False)
