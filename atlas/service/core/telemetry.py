from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.core.ssh import SSHRunner
from atlas.atlas.core.telemetry.agent import INSTALL_TIMEOUT_SECONDS, DatumAgent
from atlas.atlas.core.telemetry.collectors import SERVICE_DOCTYPES
from atlas.atlas.core.telemetry.datum import get_datum_url
from atlas.metal_server.core.host_installation import throw_script_failure


class ServiceTelemetry:
	"""Install the Datum agent on the VM of a service node. The VM sends under its own name."""

	def __init__(self, node: Document) -> None:
		self.node = node
		self.service = SERVICE_DOCTYPES[node.doctype]
		self.virtual_machine = frappe.get_doc("Virtual Machine", node.virtual_machine)

	def configure(self) -> None:
		runner = SSHRunner(
			self.virtual_machine.ssh_host, proxy_command=self.virtual_machine.get_ssh_proxy_command()
		)
		result = DatumAgent(runner, self.virtual_machine.name, "atlas").install(role=self.service)
		if not result.is_success:
			throw_script_failure(
				_("Could not install the Datum agent on {0}.").format(self.node.name), result
			)


def enqueue_service_telemetry(node: Document) -> None:
	"""Queue the telemetry setup of one node. A queued setup absorbs a second request."""
	if not get_datum_url():
		return

	frappe.enqueue(
		configure_service_telemetry,
		queue="long",
		timeout=INSTALL_TIMEOUT_SECONDS,
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
