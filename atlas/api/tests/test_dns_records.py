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
			get_dns_provider.return_value.set_service_addresses.side_effect = Route53Error("Throttling")
			status, body = error_body(set_dns_record, name="internal-s3-svc.example.com")

		self.assertEqual(status, 502)
		self.assertEqual(body["error"], {"code": "dns_provider_error", "message": "Throttling", "fields": []})


class TestDnsRecordPayload(UnitTestCase):
	def test_an_address_alone_keeps_the_address_record(self) -> None:
		payload = DnsRecordPayload.model_validate({"address": "52.95.110.1"})

		self.assertIsNone(payload.type)
		self.assertEqual(str(payload.address), "52.95.110.1")

	def test_addresses_take_a_health_check(self) -> None:
		payload = DnsRecordPayload.model_validate(
			{
				"addresses": ["52.95.110.1", "52.95.110.2"],
				"health_check_url": "https://s3-admin-svc.example.com/health",
			}
		)

		self.assertEqual(
			[str(address) for address in payload.service_addresses], ["52.95.110.1", "52.95.110.2"]
		)

	def test_the_route_sends_every_address_and_the_health_check(self) -> None:
		body = {"addresses": ["52.95.110.1"], "health_check_url": "https://s3-admin-svc.example.com/health"}
		with (
			api_request(
				"PUT",
				"/api/atlas/dns-records/s3-svc.example.com",
				tenant_id=0,
				send_tenant_header=True,
				json=body,
			),
			patch("atlas.api.routes.dns_records.get_dns_provider") as get_dns_provider,
		):
			set_dns_record(name="s3-svc.example.com")

		name, addresses, ttl, url = get_dns_provider.return_value.set_service_addresses.call_args.args
		self.assertEqual(
			(name, [str(address) for address in addresses], ttl), ("s3-svc.example.com", ["52.95.110.1"], 300)
		)
		self.assertEqual(url, "https://s3-admin-svc.example.com/health")

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
			{"address": "52.95.110.1", "addresses": ["52.95.110.2"]},
			{"addresses": []},
			{"addresses": ["52.95.110.1", "52.95.110.1"]},
			{"addresses": ["52.95.110.1", "fdaa:1::7"]},
			{"type": "AAAA", "addresses": ["52.95.110.1"]},
			{"addresses": ["fdaa:1::7"], "health_check_url": "https://s3-admin-svc.example.com/health"},
			{"addresses": ["52.95.110.1"], "health_check_url": "http://s3-admin-svc.example.com/health"},
			{
				"addresses": ["52.95.110.1"],
				"health_check_url": "https://s3-admin-svc.example.com:8443/health",
			},
			{"type": "TXT", "values": ["value"], "addresses": ["52.95.110.1"]},
		):
			with self.subTest(body=body), self.assertRaises(ValidationError):
				DnsRecordPayload.model_validate(body)
