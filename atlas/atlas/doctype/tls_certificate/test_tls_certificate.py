"""Unit tests for the TLS Certificate controller — the cert→proxy wiring, the
status machine, common_name derivation, and the renewal window.

The two external edges are mocked: the TLS provider's `issue()` (so no certbot)
and `proxy.push_cert` (so no SSH). What's left — the controller's own logic — is
fully exercised: it resolves the domain's providers, records the result, and fans
the PEMs out to every proxy VM.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.tls_certificate import tls_certificate as module
from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.atlas.tls.base import IssuedCert


def _set_provider_types() -> None:
	frappe.db.set_single_value("Atlas Settings", "dns_provider_type", "Route53")
	frappe.db.set_single_value("Atlas Settings", "tls_provider_type", "Let's Encrypt")


def _make_domain(domain: str, region: str):
	_set_provider_types()
	if frappe.db.exists("Root Domain", domain):
		# A Root Domain with this name may survive from another suite (test_site
		# seeds blr1.frappe.dev with its OWN provider types). Re-point it at THIS
		# suite's types so the denormalized tls_provider_type assertion sees
		# Let's Encrypt, not whatever the neighbour seeded.
		existing = frappe.get_doc("Root Domain", domain)
		existing.db_set("dns_provider_type", "Route53")
		existing.db_set("tls_provider_type", "Let's Encrypt")
		return existing
	return frappe.get_doc(
		{
			"doctype": "Root Domain",
			"domain": domain,
			"region": region,
			"dns_provider_type": "Route53",
			"tls_provider_type": "Let's Encrypt",
		}
	).insert(ignore_permissions=True)


def _purge_vms() -> None:
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestCommonName(IntegrationTestCase):
	def setUp(self) -> None:
		_make_domain("blr1.frappe.dev", "blr1")

	def test_common_name_is_wildcard_of_domain(self) -> None:
		cert = frappe.get_doc({"doctype": "TLS Certificate", "root_domain": "blr1.frappe.dev"}).insert(
			ignore_permissions=True
		)
		self.assertEqual(cert.common_name, "*.blr1.frappe.dev")

	def test_tls_provider_denormalized_from_domain(self) -> None:
		cert = frappe.get_doc({"doctype": "TLS Certificate", "root_domain": "blr1.frappe.dev"}).insert(
			ignore_permissions=True
		)
		self.assertEqual(cert.tls_provider_type, "Let's Encrypt")


class TestIssueAndPush(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_test_server()
		_ensure_test_image()
		_purge_vms()
		_make_domain("blr1.frappe.dev", "blr1")
		# Two PEM files on disk — issue() records their paths; _push_to_proxies reads them.
		self._tmp = tempfile.TemporaryDirectory()
		self.fullchain = Path(self._tmp.name) / "fullchain.pem"
		self.privkey = Path(self._tmp.name) / "privkey.pem"
		self.fullchain.write_text("FULLCHAIN-PEM")
		self.privkey.write_text("PRIVKEY-PEM")

	def tearDown(self) -> None:
		self._tmp.cleanup()

	def _issued(self) -> IssuedCert:
		# not_before/not_after are RAW OpenSSL date strings, exactly as
		# issue-cert.py emits them from `openssl x509 -dates` — NOT pre-normalized.
		# The controller must parse these to its Datetime columns; a real LE issuance
		# crashed MySQL with 'Incorrect datetime value' when it didn't.
		return IssuedCert(
			fullchain_path=str(self.fullchain),
			privkey_path=str(self.privkey),
			not_before="Jun  8 00:00:00 2026 GMT",
			not_after="Sep  6 00:00:00 2026 GMT",
		)

	def test_issue_records_result_and_pushes_to_all_proxies(self) -> None:
		# Two proxies (both push targets), one non-proxy (excluded).
		proxy_a = _new_vm(is_proxy=1).name
		proxy_b = _new_vm(is_proxy=1).name
		_new_vm(is_proxy=0)

		cert = frappe.get_doc({"doctype": "TLS Certificate", "root_domain": "blr1.frappe.dev"}).insert(
			ignore_permissions=True
		)

		fake_tls = patch.object(module.tls, "for_tls_provider_type")
		fake_dns = patch.object(module.dns, "for_dns_provider_type")
		fake_push = patch.object(module.proxy, "push_cert")
		with fake_tls as tls_for, fake_dns, fake_push as push:
			tls_for.return_value.issue.return_value = self._issued()
			cert.issue()

		cert.reload()
		self.assertEqual(cert.status, "Active")
		self.assertEqual(cert.fullchain_path, str(self.fullchain))
		# The raw OpenSSL strings are normalized to the Datetime columns.
		self.assertEqual(str(cert.issued_on), "2026-06-08 00:00:00")
		self.assertEqual(str(cert.expires_on), "2026-09-06 00:00:00")

		# push_cert called exactly for the two proxies, with the PEM BYTES.
		pushed_vms = {call.args[0] for call in push.call_args_list}
		self.assertEqual(pushed_vms, {proxy_a, proxy_b})
		for call in push.call_args_list:
			self.assertEqual(call.args[1], "FULLCHAIN-PEM")
			self.assertEqual(call.args[2], "PRIVKEY-PEM")

	def test_issue_failure_flips_status_to_failed(self) -> None:
		cert = frappe.get_doc({"doctype": "TLS Certificate", "root_domain": "blr1.frappe.dev"}).insert(
			ignore_permissions=True
		)
		with patch.object(module.tls, "for_tls_provider_type") as tls_for:
			tls_for.return_value.issue.side_effect = frappe.ValidationError("boom")
			with self.assertRaises(frappe.ValidationError):
				cert.issue()
		cert.reload()
		self.assertEqual(cert.status, "Failed")

	def test_push_to_proxies_requires_issued_paths(self) -> None:
		cert = frappe.get_doc({"doctype": "TLS Certificate", "root_domain": "blr1.frappe.dev"}).insert(
			ignore_permissions=True
		)
		with self.assertRaises(frappe.ValidationError):
			cert.push_to_proxies()

	def test_one_unreachable_proxy_does_not_block_the_rest(self) -> None:
		good = _new_vm(is_proxy=1).name
		bad = _new_vm(is_proxy=1).name
		cert = frappe.get_doc(
			{
				"doctype": "TLS Certificate",
				"root_domain": "blr1.frappe.dev",
				"fullchain_path": str(self.fullchain),
				"privkey_path": str(self.privkey),
				"status": "Active",
			}
		).insert(ignore_permissions=True)

		def push(vm, fullchain, privkey):
			if vm == bad:
				raise frappe.ValidationError("guest down")

		with patch.object(module.proxy, "push_cert", side_effect=push):
			pushed = cert.push_to_proxies()
		# The good proxy still got the cert; the bad one was logged and skipped.
		self.assertEqual(pushed, [good])


class TestRenewExpiring(IntegrationTestCase):
	def setUp(self) -> None:
		_make_domain("blr1.frappe.dev", "blr1")

	def test_renews_only_certs_inside_the_window(self) -> None:
		soon = frappe.utils.add_days(frappe.utils.now_datetime(), 10)
		far = frappe.utils.add_days(frappe.utils.now_datetime(), 200)
		near = frappe.get_doc(
			{
				"doctype": "TLS Certificate",
				"root_domain": "blr1.frappe.dev",
				"status": "Active",
				"expires_on": soon,
			}
		).insert(ignore_permissions=True)
		distant = frappe.get_doc(
			{
				"doctype": "TLS Certificate",
				"root_domain": "blr1.frappe.dev",
				"status": "Active",
				"expires_on": far,
			}
		).insert(ignore_permissions=True)

		renewed: list[str] = []
		with patch.object(module.TLSCertificate, "renew", lambda self: renewed.append(self.name)):
			module.renew_expiring()

		self.assertIn(near.name, renewed)
		self.assertNotIn(distant.name, renewed)
