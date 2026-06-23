"""Unit tests for the Root Domain controller — autoname, immutability, the
`*.<domain>` derivation, and the issue_certificate orchestration (find-or-create
the single TLS Certificate, then delegate issuance to it)."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.tls_certificate import tls_certificate as cert_module


def _make(domain: str, region: str):
	return frappe.get_doc(
		{
			"doctype": "Root Domain",
			"domain": domain,
			"region": region,
			"domain_provider_type": "Route53",
			"tls_provider_type": "Let's Encrypt",
		}
	).insert(ignore_permissions=True)


class TestRootDomain(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.db.set_single_value("Route53 Settings", "domain_provider_type", "Route53")
		frappe.db.set_single_value("Atlas Settings", "tls_provider_type", "Let's Encrypt")
		for name in frappe.get_all("TLS Certificate", pluck="name"):
			frappe.delete_doc("TLS Certificate", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Root Domain", pluck="name"):
			frappe.delete_doc("Root Domain", name, force=1, ignore_permissions=True)

	def test_autoname_is_the_domain(self) -> None:
		domain = _make("blr1.frappe.dev", "blr1")
		self.assertEqual(domain.name, "blr1.frappe.dev")

	def test_common_name_is_wildcard(self) -> None:
		domain = _make("nyc3.frappe.dev", "nyc3")
		self.assertEqual(domain.common_name, "*.nyc3.frappe.dev")

	def test_domain_and_region_immutable_after_insert(self) -> None:
		domain = _make("blr1.frappe.dev", "blr1")
		domain.region = "nyc3"
		with self.assertRaises(frappe.ValidationError):
			domain.save(ignore_permissions=True)

	def test_issue_certificate_creates_and_delegates(self) -> None:
		domain = _make("blr1.frappe.dev", "blr1")
		with patch.object(cert_module.TLSCertificate, "issue", lambda self: None) as _:
			cert_name = domain.issue_certificate()
		cert = frappe.get_doc("TLS Certificate", cert_name)
		self.assertEqual(cert.root_domain, "blr1.frappe.dev")
		self.assertEqual(cert.tls_provider_type, "Let's Encrypt")

	def test_issue_certificate_reuses_existing_cert(self) -> None:
		domain = _make("blr1.frappe.dev", "blr1")
		with patch.object(cert_module.TLSCertificate, "issue", lambda self: None):
			first = domain.issue_certificate()
			second = domain.issue_certificate()
		self.assertEqual(first, second)
		self.assertEqual(frappe.db.count("TLS Certificate", {"root_domain": "blr1.frappe.dev"}), 1)

	def test_types_denormalized_from_settings_when_omitted(self) -> None:
		# before_insert fills the types from the active Settings singles (set in setUp).
		domain = frappe.get_doc(
			{"doctype": "Root Domain", "domain": "den1.frappe.dev", "region": "den1"}
		).insert(ignore_permissions=True)
		self.assertEqual(domain.domain_provider_type, "Route53")
		self.assertEqual(domain.tls_provider_type, "Let's Encrypt")

	def test_blank_types_fail_loud(self) -> None:
		# With the Settings singles unset, the denormalization leaves blanks and the
		# require-guard throws at save with a clear message (not a cryptic issuance error).
		frappe.db.set_single_value("Route53 Settings", "domain_provider_type", "")
		frappe.db.set_single_value("Atlas Settings", "tls_provider_type", "")
		with self.assertRaises(frappe.ValidationError) as raised:
			frappe.get_doc(
				{"doctype": "Root Domain", "domain": "blank1.frappe.dev", "region": "blank1"}
			).insert(ignore_permissions=True)
		self.assertIn("provider_type", str(raised.exception))
