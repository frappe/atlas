from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.core.ssh import SSHRunner
from atlas.atlas.core.telemetry.collectors import SERVICE_DOCTYPES
from atlas.atlas.core.telemetry.datum import HOST_TOKEN_LIFETIME, get_datum_url, issue_datum_token
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask
from atlas.metal_server.core.host_installation import throw_script_failure

TELEMETRY_INSTALL_TIMEOUT_SECONDS = 600


class ServiceTelemetry:
	"""Send a service node's system metrics and logs to Datum through Vector, under the node's own name."""

	def __init__(self, node: Document) -> None:
		self.node = node
		self.service = SERVICE_DOCTYPES[node.doctype]
		self.virtual_machine = frappe.get_doc("Virtual Machine", node.virtual_machine)

	def configure(self) -> None:
		"""Install Vector, then a fresh Datum token. The token goes over direct SSH, so no SSH Task stores it."""
		packages = SSHTask.create_for_script_file(
			target_type="Virtual Machine",
			target=self.virtual_machine.name,
			script_path="install-vector.sh",
			timeout_seconds=TELEMETRY_INSTALL_TIMEOUT_SECONDS,
			run_in_background=False,
		).result
		if not packages or not packages.is_success:
			throw_script_failure(_("Could not install Vector on {0}.").format(self.node.name), packages)

		result = SSHRunner(
			self.virtual_machine.ssh_host, proxy_command=self.virtual_machine.get_ssh_proxy_command()
		).run_script(
			"install-service-telemetry.sh",
			data={
				"DATUM_URL": get_datum_url(),
				"DATUM_TOKEN": issue_datum_token(self.node.name, HOST_TOKEN_LIFETIME),
				"SERVICE": self.service,
			},
			timeout_seconds=TELEMETRY_INSTALL_TIMEOUT_SECONDS,
		)
		if not result.is_success:
			throw_script_failure(_("Could not configure telemetry on {0}.").format(self.node.name), result)


def enqueue_service_telemetry(node: Document) -> None:
	"""Queue the telemetry setup of one node. A queued setup absorbs a second request."""
	if not get_datum_url():
		return

	frappe.enqueue(
		configure_service_telemetry,
		queue="long",
		timeout=TELEMETRY_INSTALL_TIMEOUT_SECONDS,
		doctype=node.doctype,
		name=node.name,
		job_id=f"atlas||service-telemetry||{node.doctype}||{node.name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def request_service_telemetry(node: Document) -> None:
	"""Queue the telemetry setup of an active node for an operator."""
	if node.status != "Active":
		frappe.throw(_("{0} is not active.").format(node.name))
	if not get_datum_url():
		frappe.throw(_("Set datum_url in the site config to send telemetry."))

	enqueue_service_telemetry(node)


def configure_service_telemetry(doctype: str, name: str) -> None:
	node = frappe.get_doc(doctype, name)
	if node.status == "Active":
		ServiceTelemetry(node).configure()


def renew_service_telemetry_credentials() -> None:
	"""Queue a telemetry setup with a fresh Datum token for every active service node. Runs daily."""
	for doctype in SERVICE_DOCTYPES:
		for name in frappe.get_all(doctype, filters={"status": "Active"}, pluck="name"):
			enqueue_service_telemetry(frappe.get_doc(doctype, name))
