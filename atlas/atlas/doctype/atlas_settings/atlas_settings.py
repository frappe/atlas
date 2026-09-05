# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document

if TYPE_CHECKING:
	from atlas.atlas.core.dns_providers.base import DnsProvider
	from atlas.atlas.core.server_providers.base import ServerProvider
	from atlas.atlas.s3 import S3Client


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
		metald_binary_x86_64_file: DF.Link | None
		metald_source_hash: DF.Data | None
		private_network_cidr: DF.Data
		private_network_mtu: DF.Int
		public_ssh_key: DF.SmallText
		region_id: DF.Int
		region_name: DF.Data
		route53_access_key_id: DF.Data | None
		route53_access_key_secret: DF.Password | None
		route53_dns_zone_id: DF.Data | None
		s3_access_key_id: DF.Data | None
		s3_bucket: DF.Data | None
		s3_endpoint_url: DF.Data | None
		s3_region: DF.Data | None
		s3_secret_access_key: DF.Password | None
		s3_signed_url_expiry: DF.Int
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
		wg_mesh_binary_x86_64_file: DF.Link | None
		wg_mesh_source_hash: DF.Data | None
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

	def get_s3_client(self) -> "S3Client":
		"""Create the configured S3 client."""
		from atlas.atlas.s3 import S3Client

		return S3Client(
			bucket=self.s3_bucket,
			access_key_id=self.s3_access_key_id,
			secret_access_key=self.get_password("s3_secret_access_key", raise_exception=False),
			endpoint_url=self.s3_endpoint_url or "",
			region=self.s3_region or "",
			signed_url_expiry=self.s3_signed_url_expiry or 86400,
		)

	def validate(self) -> None:
		if self.is_setup_completed and not (
			self.is_server_provider_setup_completed and self.is_dns_setup_completed
		):
			frappe.throw(_("Atlas Settings cannot be marked as completed before provider setup is complete."))

		self.server_provider_controller.validate_settings()
		self.dns_provider_controller.validate_settings()

		if self.wildcard_domain.startswith("*."):
			frappe.throw(
				_(
					"Remove '*.' from the wildcard domain in Atlas Settings. It is automatically added by Atlas."
				)
			)

		self.region_name = self.region_name.strip().lower()

	def on_update(self) -> None:
		"""App install creates this single document empty, so there is nothing to check then."""
		if self.flags.in_insert:
			return

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
			self.server_provider_controller.setup_infrastructure()
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
			frappe.throw(_("Atlas Settings must be fully set up before syncing server sizes."))

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_sync_server_sizes",
			queue="default",
			job_id="atlas-sync-server-sizes",
			deduplicate=True,
			enqueue_after_commit=True,
		)
		frappe.msgprint(_("Server sizes sync has been queued. Please check after some time."))

	def _sync_server_sizes(self) -> None:
		from atlas.server.core.catalog_sync import CatalogSynchronizer

		CatalogSynchronizer(self.server_provider_controller).sync_server_sizes()

	@frappe.whitelist(methods=["POST"])
	def sync_server_images(self) -> None:
		frappe.only_for("System Manager")
		if not self.is_setup_completed:
			frappe.throw(_("Atlas Settings must be fully set up before syncing server images."))

		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_sync_server_images",
			queue="default",
			job_id="atlas-sync-server-images",
			deduplicate=True,
			enqueue_after_commit=True,
		)
		frappe.msgprint(_("Server images sync has been queued. Please check after some time."))

	def _sync_server_images(self) -> None:
		from atlas.server.core.catalog_sync import CatalogSynchronizer

		CatalogSynchronizer(self.server_provider_controller).sync_server_images()
