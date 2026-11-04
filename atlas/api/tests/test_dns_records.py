from __future__ import annotations

from unittest.mock import patch

from frappe.tests import UnitTestCase

from atlas.api.routes.dns_records import set_dns_record
from atlas.api.tests.test_support import TENANT_ID, api_request, error_body
from atlas.atlas.core.dns_providers.route53 import Route53Error

PATH = "/api/atlas/dns-records/internal-s3-svc.example.com"
PAYLOAD = {"address": "fdaa:1::7"}


class TestDnsRecordRoutes(UnitTestCase):
	def test_dns_records_need_tenant_zero(self) -> None:
		with (
			api_request("PUT", PATH, tenant_id=TENANT_ID, send_tenant_header=True, json=PAYLOAD),
			patch("atlas.api.routes.dns_records.get_dns_provider") as get_dns_provider,
		):
			status, _ = error_body(set_dns_record, name="internal-s3-svc.example.com")

		self.assertIn(status, (400, 403))
		get_dns_provider.assert_not_called()

	def test_dns_provider_error_reaches_the_caller(self) -> None:
		with (
			api_request("PUT", PATH, tenant_id=0, send_tenant_header=True, json=PAYLOAD),
			patch("atlas.api.routes.dns_records.get_dns_provider") as get_dns_provider,
		):
			get_dns_provider.return_value.upsert_service_record.side_effect = Route53Error("Throttling")
			status, body = error_body(set_dns_record, name="internal-s3-svc.example.com")

		self.assertEqual(status, 502)
		self.assertEqual(body["error"], {"code": "dns_provider_error", "message": "Throttling", "fields": []})
