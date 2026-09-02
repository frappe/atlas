# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

import frappe
from frappe.model.document import Document

if TYPE_CHECKING:
	from atlas.atlas.core.dns_providers.base import DnsProvider
	from atlas.atlas.core.server_providers.base import ServerProvider


class AtlasSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		dns_provider: DF.Literal["Route53"]
		is_dns_setup_completed: DF.Check
		is_server_provider_setup_completed: DF.Check
		is_setup_completed: DF.Check
		metald_binary_x86_64_download_url: DF.Data
		private_network_cidr: DF.Data
		private_network_mtu: DF.Int
		public_ssh_key: DF.SmallText
		region_id: DF.Int
		region_name: DF.Data
		route53_access_key_id: DF.Data | None
		route53_access_key_secret: DF.Password | None
		route53_dns_zone_id: DF.Data | None
		scaleway_access_key: DF.Data | None
		scaleway_machine_billing_cycle: DF.Literal["Hourly", "Monthly"]
		scaleway_organization_id: DF.Data | None
		scaleway_private_network_id: DF.Data | None
		scaleway_project_id: DF.Data | None
		scaleway_secret_key: DF.Password | None
		scaleway_ssh_key_id: DF.Data | None
		scaleway_vpc_id: DF.Data | None
		scaleway_zone: DF.Literal[
			"fr-par-1",
			"fr-par-2",
			"fr-par-3",
			"nl-ams-1",
			"nl-ams-2",
			"nl-ams-3",
			"pl-waw-1",
			"pl-waw-2",
			"pl-waw-3",
		]
		server_provider: DF.Literal["Scaleway"]
		wildcard_domain: DF.Data
	# end: auto-generated types

	@property
	def resource_name_prefix(self) -> str:
		return f"atlas-{self.region_name.lower()}-"

	@cached_property
	def server_provider_controller(self) -> "ServerProvider":
		from atlas.atlas.core.server_providers import get_server_provider

		return get_server_provider(settings=self)

	@cached_property
	def dns_provider_controller(self) -> "DnsProvider":
		from atlas.atlas.core.dns_providers import get_dns_provider

		return get_dns_provider(settings=self)

	def validate(self) -> None:
		if self.is_setup_completed and not (
			self.is_server_provider_setup_completed and self.is_dns_setup_completed
		):
			frappe.throw("Atlas Settings cannot be marked as completed before provider setup is complete.")

		self.server_provider_controller.validate_settings()
		self.dns_provider_controller.validate_settings()

		if self.wildcard_domain.startswith("*."):
			frappe.throw(
				"Remove '*.' from the wildcard domain in Atlas Settings. It is automatically added by Atlas."
			)

		self.region_name = self.region_name.strip().lower()

	def on_update(self) -> None:
		if any(self.has_value_changed(field) for field in self.server_provider_controller.credential_fields):
			self.server_provider_controller.validate_credentials()

		if any(self.has_value_changed(field) for field in self.dns_provider_controller.credential_fields):
			self.dns_provider_controller.validate_credentials()

	def before_save(self) -> None:
		if (
			self.is_dns_setup_completed
			and self.is_server_provider_setup_completed
			and not self.is_setup_completed
		):
			self.is_setup_completed = True

	@frappe.whitelist(methods=["POST"])
	def setup_server_provider(self) -> None:
		frappe.only_for("System Manager")
		try:
			self.server_provider_controller.bootstrap()
		except Exception:
			# Commit any changes to the database before re-raising the exception
			# to avoid losing the setup progress.
			frappe.db.commit()  # nosemgrep
			raise

	@frappe.whitelist(methods=["POST"])
	def setup_dns_provider(self) -> None:
		frappe.only_for("System Manager")
		self.dns_provider_controller.bootstrap()

	@frappe.whitelist(methods=["POST"])
	def sync_server_sizes(self) -> None:
		frappe.only_for("System Manager")
		if not self.is_setup_completed:
			frappe.throw("Atlas Settings must be fully set up before syncing server sizes.")

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_sync_server_sizes",
			queue="default",
			job_id="atlas-sync-server-sizes",
			deduplicate=True,
		)
		frappe.msgprint("Server sizes sync has been queued. Please check after some time.")

	def _sync_server_sizes(self) -> None:
		self.server_provider_controller.sync_provider_sizes()

	@frappe.whitelist(methods=["POST"])
	def sync_server_images(self) -> None:
		frappe.only_for("System Manager")
		if not self.is_setup_completed:
			frappe.throw("Atlas Settings must be fully set up before syncing server images.")

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_sync_server_images",
			queue="default",
			job_id="atlas-sync-server-images",
			deduplicate=True,
		)
		frappe.msgprint("Server images sync has been queued. Please check after some time.")

	def _sync_server_images(self) -> None:
		self.server_provider_controller.sync_provider_images()
