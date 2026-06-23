"""Root Domain DocType — one wildcard zone == one region.

A `Root Domain` row (`blr1.frappe.dev`) owns the regional wildcard cert
(`*.blr1.frappe.dev`) that fronts the proxy fleet in `region`. The controller is
a thin orchestrator: `issue_certificate()` locates (or creates) the domain's
single `TLS Certificate` and delegates issuance to it. The cert→proxy push lives
on `TLS Certificate` ([tls_certificate.py](../tls_certificate/tls_certificate.py)).
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document

IMMUTABLE_AFTER_INSERT = ("domain", "region")


class RootDomain(Document):
	def before_insert(self) -> None:
		self._denormalize_provider_types()

	def _denormalize_provider_types(self) -> None:
		"""Freeze the active DNS / TLS vendor types onto the row, so a later vendor
		switch on the Settings singles never re-points an existing region's issuance.
		Mirrors `Server.provider_type`."""
		if not self.domain_provider_type:
			self.domain_provider_type = frappe.db.get_single_value("Route53 Settings", "domain_provider_type")
		if not self.tls_provider_type:
			self.tls_provider_type = frappe.db.get_single_value("Atlas Settings", "tls_provider_type")

	def validate(self) -> None:
		self._require_provider_types()
		self._validate_immutability()

	def _require_provider_types(self) -> None:
		"""Fail loud at save if a vendor type is still blank — otherwise issuance fails
		far later with a cryptic 'No implementation for provider_type None'. The types
		come from the active Settings singles (denormalized in before_insert), so a
		blank means they were never configured."""
		from frappe import _

		if not self.domain_provider_type:
			frappe.throw(_("Set Route53 Settings.domain_provider_type before creating a Root Domain"))
		if not self.tls_provider_type:
			frappe.throw(_("Set Atlas Settings.tls_provider_type before creating a Root Domain"))

	def _validate_immutability(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(self, field) != getattr(original, field):
				frappe.throw(f"{field} is immutable after insert")

	@property
	def common_name(self) -> str:
		"""The wildcard the cert certifies: `*.<domain>`."""
		return f"*.{self.domain}"

	@frappe.whitelist()
	def issue_certificate(self) -> str:
		"""Issue / Renew Certificate button. Find or create this domain's single
		TLS Certificate, then run its issue flow. Returns the cert's name."""
		cert = self._get_or_create_certificate()
		cert.issue()
		return cert.name

	def _get_or_create_certificate(self):
		existing = frappe.db.get_value("TLS Certificate", {"root_domain": self.name}, "name")
		if existing:
			return frappe.get_doc("TLS Certificate", existing)
		cert = frappe.get_doc(
			{
				"doctype": "TLS Certificate",
				"root_domain": self.name,
				"tls_provider_type": self.tls_provider_type,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)
		return cert
