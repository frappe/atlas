from __future__ import annotations

from unittest.mock import patch

from frappe.tests import UnitTestCase
from pydantic import ValidationError

from atlas.api.models import DnsRecordPayload
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


class TestDnsRecordPayload(UnitTestCase):
	def test_an_address_alone_keeps_the_address_record(self) -> None:
		payload = DnsRecordPayload.model_validate({"address": "52.95.110.1"})

		self.assertIsNone(payload.type)
		self.assertEqual(str(payload.address), "52.95.110.1")

	def test_txt_and_cname_records_take_values(self) -> None:
		txt = DnsRecordPayload.model_validate({"type": "TXT", "values": ["v=1; a-B_9"], "ttl": 60})
		cname = DnsRecordPayload.model_validate({"type": "CNAME", "values": ["vm-1.example.net."]})

		self.assertEqual(txt.values, ["v=1; a-B_9"])
		self.assertEqual(cname.values, ["vm-1.example.net."])

	def test_records_that_do_not_suit_their_type_are_refused(self) -> None:
		for body in (
			{},
			{"type": "TXT"},
			{"type": "TXT", "values": []},
			{"type": "TXT", "values": ["value"], "address": "52.95.110.1"},
			{"type": "TXT", "values": ['"quoted"']},
			{"type": "TXT", "values": ["back\\slash"]},
			{"type": "TXT", "values": ["a" * 256]},
			{"type": "CNAME", "values": ["a.example.net", "b.example.net"]},
			{"type": "CNAME", "values": ["not a host"]},
			{"type": "CNAME", "values": ["a..example.net"]},
			{"type": "A"},
			{"type": "A", "address": "2600:1f18::7"},
			{"type": "AAAA", "address": "52.95.110.1"},
			{"address": "52.95.110.1", "values": ["value"]},
			{"type": "MX", "values": ["mail.example.net"]},
		):
			with self.subTest(body=body), self.assertRaises(ValidationError):
				DnsRecordPayload.model_validate(body)
