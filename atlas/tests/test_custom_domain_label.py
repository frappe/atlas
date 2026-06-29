"""Tests for custom-domain FQDN rules (spec/18 Phase 2) — the full-FQDN sibling of
subdomain_label. No host, no DB: pure validation.

- normalize_domain lowercases, strips a trailing dot and surrounding whitespace.
- validate_custom_domain accepts a well-formed external FQDN and rejects: a bare label
  (no dot — that's the register(label) wildcard path), a name UNDER the active regional
  wildcard (belongs in the wildcard path), an empty/over-long label, a leading/trailing
  hyphen, non-DNS characters, and an over-long total.
"""

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.custom_domain_label import normalize_domain, validate_custom_domain

REGION = "blr1.frappe.dev"


class TestNormalizeDomain(IntegrationTestCase):
	def test_lowercases_and_strips(self) -> None:
		self.assertEqual(normalize_domain("  Shop.ACME.com.  "), "shop.acme.com")

	def test_trailing_dot_removed(self) -> None:
		self.assertEqual(normalize_domain("shop.acme.com."), "shop.acme.com")

	def test_none_is_empty(self) -> None:
		self.assertEqual(normalize_domain(None), "")


class TestValidateCustomDomain(IntegrationTestCase):
	def _invalid(self, domain: str) -> None:
		with self.assertRaises(frappe.ValidationError):
			validate_custom_domain(domain, REGION)

	def test_accepts_a_well_formed_external_fqdn(self) -> None:
		validate_custom_domain("shop.acme.com", REGION)  # no raise
		validate_custom_domain("a.b.c.example.co.uk", REGION)
		validate_custom_domain("my-shop.acme.com", REGION)  # hyphen mid-label is fine

	def test_bare_label_rejected(self) -> None:
		# No dot — that's a register(label) wildcard subdomain, not a custom domain.
		self._invalid("shop")

	def test_empty_rejected(self) -> None:
		self._invalid("")
		self._invalid(None)

	def test_name_under_regional_wildcard_rejected(self) -> None:
		# A name under *.<region> is a Subdomain; routing it as a custom domain would split
		# the route across two tables and issue a redundant cert.
		self._invalid(f"app.{REGION}")
		self._invalid(REGION)  # the bare region suffix itself

	def test_name_outside_wildcard_with_similar_suffix_accepted(self) -> None:
		# Endswith the region label but NOT under the dotted suffix — a real external name.
		validate_custom_domain("notblr1.frappe.dev.evil.com", REGION)

	def test_empty_label_rejected(self) -> None:
		self._invalid("shop..acme.com")  # doubled dot
		self._invalid(".acme.com")  # leading dot

	def test_leading_or_trailing_hyphen_rejected(self) -> None:
		self._invalid("-shop.acme.com")
		self._invalid("shop-.acme.com")

	def test_non_dns_characters_rejected(self) -> None:
		self._invalid("shop_underscore.acme.com")
		self._invalid("shop!.acme.com")

	def test_over_long_label_rejected(self) -> None:
		self._invalid("a" * 64 + ".acme.com")

	def test_over_long_domain_rejected(self) -> None:
		# Many max-length labels exceed 253 total.
		self._invalid(".".join(["a" * 60] * 5) + ".com")

	def test_no_region_does_not_block_anything_under_a_suffix(self) -> None:
		# An empty region_domain means no wildcard guard — any well-formed FQDN passes.
		validate_custom_domain("app.blr1.frappe.dev", "")
