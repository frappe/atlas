from __future__ import annotations

import logging
import random
import secrets
import string
import time
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING

import frappe
import requests
from frappe import _

from atlas.atlas.core.artifacts import get_internal_base_url
from atlas.atlas.core.ssh import wait_for_server
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask
from atlas.auth.issuer import issue_token
from atlas.service.core.cargo.bucket import enqueue_bucket_provisioning
from atlas.service.core.cargo.storage_cluster import storage_cluster_config_json
from atlas.service.core.cargo.telemetry import telemetry_config_json
from atlas.service.doctype.cargo_server.cargo_server import cargo_lifecycle_lock

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.service.doctype.cargo_server.cargo_server import CargoServer
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine

INSTALL_TIMEOUT_SECONDS = 3_600
SSH_TIMEOUT_SECONDS = 600
SSH_POLL_INTERVAL_SECONDS = 5
READINESS_TIMEOUT_SECONDS = 120
READINESS_POLL_INTERVAL_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 5
TOKEN_LIFETIME = timedelta(days=365)
PASSWORD_LENGTH = 32
PASSWORD_SYMBOLS = "!@#$%^&*()-_=+"
PROXY_SITE_NAMES = ("cargo", "cargo-pilot")


class CargoServerProvisioner:
	"""Install Cargo and publish its mesh routes."""

	def __init__(self, cargo_server: CargoServer) -> None:
		self.cargo_server = cargo_server
		self.logger = logging.getLogger("atlas.service.provisioning")

	def run(self) -> None:
		"""Run each Cargo setup step in order."""
		with cargo_lifecycle_lock():
			self.cargo_server = frappe.get_single("Cargo Server")
			if self.cargo_server.status == "Failed" or not self.is_virtual_machine_ready:
				return

			phase = "start"
			self.cargo_server.status = "Provisioning"
			self.cargo_server.failure_message = None
			self.save()
			try:
				for phase, operation in self.steps:
					self.logger.info(
						"Cargo provisioning step started",
						extra={"resource": self.cargo_server.name, "operation": "provision", "phase": phase},
					)
					operation()

				self.cargo_server.status = "Active"
				self.save()
				enqueue_bucket_provisioning()
			except Exception as error:
				self.cargo_server.status = "Failed"
				self.cargo_server.failure_message = f"{phase}: {error}"
				self.save()
				frappe.db.commit()  # nosemgrep
				self.logger.exception(
					"Cargo provisioning failed",
					extra={"resource": self.cargo_server.name, "operation": "provision", "phase": phase},
				)
				frappe.log_error(title=f"Cargo provisioning failed during {phase}")
				raise

	@property
	def steps(self) -> tuple[tuple[str, Callable[[], None]], ...]:
		"""Return setup steps in execution order."""
		return (
			("secure-shell", self.wait_for_ssh),
			("installation", self.install_cargo),
			("proxy-routes", self.update_proxy_routes),
			("readiness", self.wait_for_readiness),
		)

	@property
	def is_virtual_machine_ready(self) -> bool:
		"""Report whether the attached virtual machine left the draft state."""
		if not self.cargo_server.virtual_machine:
			return False
		return not self.virtual_machine.is_draft

	@property
	def virtual_machine(self) -> VirtualMachine:
		"""Return the attached Cargo virtual machine."""
		return frappe.get_doc("Virtual Machine", self.cargo_server.virtual_machine)

	@property
	def settings(self) -> AtlasSettings:
		"""Return the regional Atlas settings."""
		return frappe.get_single("Atlas Settings")

	@property
	def proxy_url(self) -> str:
		"""Return the regional Proxy control API URL."""
		return f"https://proxy.{self.settings.wildcard_domain}"

	def wait_for_ssh(self) -> None:
		"""Wait for root SSH through the guest host."""
		wait_for_server(
			host=self.virtual_machine.ssh_host,
			users=("root",),
			timeout_seconds=SSH_TIMEOUT_SECONDS,
			poll_interval_seconds=SSH_POLL_INTERVAL_SECONDS,
			proxy_command=self.virtual_machine.get_ssh_proxy_command(),
		)

	def install_cargo(self) -> None:
		"""Install Cargo with a synchronous SSH Task."""
		task = SSHTask.create_for_script_file(
			target_type="Virtual Machine",
			target=self.cargo_server.virtual_machine,
			script_path="install-cargo.sh",
			environment=self.install_environment(),
			timeout_seconds=INSTALL_TIMEOUT_SECONDS,
			run_in_background=False,
		)
		self.cargo_server.installation_task = task.name
		self.save()

		result = task.result
		if result is None or not result.is_success:
			frappe.throw(_("Cargo installation failed. See SSH Task {0}.").format(task.name))

	def install_environment(self) -> dict[str, str | int]:
		"""Issue short-lived installation values immediately before SSH."""
		settings = self.settings
		domain = self.cargo_server.domain
		atlas_token = issue_token(
			settings,
			audience=settings.admin_audience_id,
			subject="cargo",
			scope="*",
			tenant="0",
			lifetime=TOKEN_LIFETIME,
		)
		proxy_token = issue_token(
			settings,
			audience=settings.proxy_audience_id,
			subject="cargo",
			scope="site:*",
			constraints={"site": {"suffix": "-svc"}},
			lifetime=TOKEN_LIFETIME,
		)
		atlas_url = get_internal_base_url().rstrip("/")

		return {
			"PILOT_ADMIN_PASSWORD": generate_installer_password(),
			"SITE_PASSWORD": generate_installer_password(),
			"SITE": domain,
			"ADMIN_DOMAIN": self.cargo_server.pilot_domain,
			"CENTRAL_URL": "https://central.invalid",
			"CENTRAL_WEBHOOK_SECRET": "not-configured",
			"ATLAS_URL": atlas_url,
			"ATLAS_TOKEN": atlas_token,
			"ATLAS_TENANT_ID": 0,
			"JWKS_URL": settings.jwks_url,
			"CARGO_URL": f"https://{domain}",
			"REGION": settings.region_name,
			"REGION_ID": settings.region_id,
			"PROXY_URL": self.proxy_url,
			"PROXY_TOKEN": proxy_token,
			"WILDCARD_DOMAIN": settings.wildcard_domain,
			"DEFAULT_STORAGE_CLUSTER_CONFIG": storage_cluster_config_json(),
			"DEFAULT_TELEMETRY_CONFIG": telemetry_config_json(),
		}

	def update_proxy_routes(self) -> None:
		"""Map the Cargo domains to the virtual machine mesh address."""
		mesh_address = self.virtual_machine.wireguard_mesh_ipv6
		if not mesh_address:
			frappe.throw(_("The Cargo virtual machine has no mesh IPv6 address."))

		for site_name in PROXY_SITE_NAMES:
			response = requests.patch(
				f"{self.proxy_url}/v1/sites/{site_name}",
				headers=self.proxy_headers(),
				json={"address": mesh_address},
				timeout=REQUEST_TIMEOUT_SECONDS,
			)
			if not response.ok:
				frappe.throw(
					_("The Proxy control API refused the {0} route with status {1}.").format(
						site_name, response.status_code
					)
				)

	def remove_proxy_routes(self) -> None:
		"""Remove the Cargo routes before virtual machine termination."""
		for site_name in PROXY_SITE_NAMES:
			response = requests.delete(
				f"{self.proxy_url}/v1/sites/{site_name}",
				headers=self.proxy_headers(),
				timeout=REQUEST_TIMEOUT_SECONDS,
			)
			if not response.ok:
				frappe.throw(
					_("The Proxy control API refused {0} route removal with status {1}.").format(
						site_name, response.status_code
					)
				)

	def wait_for_readiness(self) -> None:
		"""Wait until Cargo answers through the regional proxy."""
		deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
		url = f"https://{self.cargo_server.domain}/api/method/ping"
		while time.monotonic() < deadline:
			try:
				response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
				if response.status_code == 200 and response.json().get("message") == "pong":
					return
			except requests.RequestException, ValueError:
				pass
			time.sleep(READINESS_POLL_INTERVAL_SECONDS)

		frappe.throw(_("Cargo did not become ready through the Proxy."))

	def proxy_headers(self) -> dict[str, str]:
		"""Return the current regional Proxy bearer credential."""
		password = self.settings.get_password("proxy_cluster_password", raise_exception=False)
		if not password:
			frappe.throw(_("Atlas Settings holds no Proxy cluster password."))

		return {"Authorization": f"Bearer {password}"}

	def save(self) -> None:
		"""Store the current Cargo Server state."""
		self.cargo_server.save(ignore_permissions=True)


def generate_installer_password() -> str:
	"""Return a password that satisfies Pilot's complexity rule."""
	characters = string.ascii_letters + string.digits + PASSWORD_SYMBOLS
	password = [
		secrets.choice(string.ascii_lowercase),
		secrets.choice(string.ascii_uppercase),
		secrets.choice(string.digits),
		secrets.choice(PASSWORD_SYMBOLS),
	]
	password.extend(secrets.choice(characters) for _ in range(PASSWORD_LENGTH - len(password)))
	random.SystemRandom().shuffle(password)
	return "".join(password)
