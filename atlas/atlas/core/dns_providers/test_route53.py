from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from frappe.tests import UnitTestCase

from atlas.atlas.core.dns_providers.base import DnsRecord, HealthCheck
from atlas.atlas.core.dns_providers.route53 import Route53Error, Route53Provider
from atlas.atlas.core.exceptions import AtlasUserError


class _FakeSettings:
	wildcard_domain = "*.example.com"
	route53_access_key_id = "AKIA_TEST"
	route53_dns_zone_id = "Z123"

	def get_password(self, fieldname: str) -> str:
		return "test-secret"


def _build_provider() -> Route53Provider:
	provider = Route53Provider(settings=_FakeSettings())
	provider.client = MagicMock()
	provider.create_zone = MagicMock(return_value="Z123")
	return provider


class TestRoute53Provider(UnitTestCase):
	def test_find_public_zone_skips_a_private_zone(self) -> None:
		provider = _build_provider()
		provider.client.list_hosted_zones_by_name.return_value = {
			"HostedZones": [
				{
					"Id": "/hostedzone/private",
					"Name": "example.com.",
					"Config": {"PrivateZone": True},
				},
				{
					"Id": "/hostedzone/public",
					"Name": "example.com.",
					"Config": {"PrivateZone": False},
				},
			],
		}

		self.assertEqual(provider.find_public_zone_id("example.com"), "public")

	def test_find_public_zone_refuses_a_private_zone(self) -> None:
		provider = _build_provider()
		provider.client.list_hosted_zones_by_name.return_value = {
			"HostedZones": [
				{
					"Id": "/hostedzone/private",
					"Name": "example.com.",
					"Config": {"PrivateZone": True},
				}
			],
		}

		self.assertIsNone(provider.find_public_zone_id("example.com"))

	def test_bootstrap_creates_the_wildcard_cname(self) -> None:
		provider = _build_provider()
		provider.settings.save = MagicMock()

		provider.bootstrap()

		change = provider.client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"][0]
		self.assertEqual(
			change["ResourceRecordSet"],
			{
				"Name": "*.example.com",
				"Type": "CNAME",
				"TTL": 3600,
				"ResourceRecords": [{"Value": "proxy.example.com"}],
			},
		)
		self.assertEqual(provider.settings.is_dns_setup_completed, 1)
		provider.settings.save.assert_called_once()

	def test_missing_health_check_is_already_removed(self) -> None:
		provider = _build_provider()
		provider.client.delete_health_check.side_effect = ClientError(
			{"Error": {"Code": "NoSuchHealthCheck", "Message": "missing"}},
			"DeleteHealthCheck",
		)

		provider.remove_health_check("health-1")

	def test_https_health_check_uses_the_proxy_domain_for_sni(self) -> None:
		provider = _build_provider()
		provider.client.create_health_check.return_value = {"HealthCheck": {"Id": "health-1"}}

		health_check_id = provider.create_health_check(
			"203.0.113.9", HealthCheck("proxy-001.example.com", "/readyz")
		)

		self.assertEqual(health_check_id, "health-1")
		configuration = provider.client.create_health_check.call_args.kwargs["HealthCheckConfig"]
		self.assertEqual(configuration["IPAddress"], "203.0.113.9")
		self.assertEqual(configuration["FullyQualifiedDomainName"], "proxy-001.example.com")
		self.assertEqual(configuration["ResourcePath"], "/readyz")
		self.assertEqual(configuration["RequestInterval"], 30)
		self.assertEqual(configuration["FailureThreshold"], 2)
		self.assertTrue(configuration["EnableSNI"])

	def test_a_gone_health_check_reads_as_none(self) -> None:
		provider = _build_provider()
		provider.client.get_health_check.side_effect = ClientError(
			{"Error": {"Code": "NoSuchHealthCheck", "Message": "missing"}}, "GetHealthCheck"
		)

		self.assertIsNone(provider.get_health_check("health-1"))

	def test_one_batch_removes_the_replaced_records_first_and_quotes_txt(self) -> None:
		provider = _build_provider()

		provider.upsert_records(
			[
				DnsRecord("s3-svc.example.com", "A", ["52.95.110.1"], 120, "52.95.110.1", "health-1"),
				DnsRecord("_acme-challenge.s3-svc.example.com", "TXT", ["token"], 60),
			],
			replaced=[DnsRecord("s3-svc.example.com", "A", ["52.95.110.9"])],
		)

		changes = provider.client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"]
		self.assertEqual([change["Action"] for change in changes], ["DELETE", "UPSERT", "UPSERT"])
		self.assertEqual(
			changes[1]["ResourceRecordSet"],
			{
				"Name": "s3-svc.example.com",
				"Type": "A",
				"TTL": 120,
				"ResourceRecords": [{"Value": "52.95.110.1"}],
				"SetIdentifier": "52.95.110.1",
				"MultiValueAnswer": True,
				"HealthCheckId": "health-1",
			},
		)
		self.assertEqual(changes[2]["ResourceRecordSet"]["ResourceRecords"], [{"Value": '"token"'}])

	def test_removing_no_records_sends_nothing(self) -> None:
		provider = _build_provider()

		provider.remove_records([])

		provider.client.change_resource_record_sets.assert_not_called()

	def test_records_of_the_name_are_read_across_pages(self) -> None:
		provider = _build_provider()
		provider.client.get_paginator.return_value.paginate.return_value = [
			{
				"ResourceRecordSets": [
					{
						"Name": "s3-svc.example.com.",
						"Type": "TXT",
						"TTL": 60,
						"ResourceRecords": [{"Value": '"token"'}],
					},
				]
			},
			{
				"ResourceRecordSets": [
					{
						"Name": "s3-svc.example.com.",
						"Type": "A",
						"TTL": 120,
						"SetIdentifier": "52.95.110.1",
						"MultiValueAnswer": True,
						"HealthCheckId": "health-1",
						"ResourceRecords": [{"Value": "52.95.110.1"}],
					},
					{
						"Name": "_x.s3-svc.example.com.",
						"Type": "TXT",
						"TTL": 60,
						"ResourceRecords": [{"Value": '"y"'}],
					},
				]
			},
		]

		self.assertEqual(
			provider.get_records("s3-svc.example.com"),
			[
				DnsRecord("s3-svc.example.com", "TXT", ["token"], 60),
				DnsRecord("s3-svc.example.com", "A", ["52.95.110.1"], 120, "52.95.110.1", "health-1"),
			],
		)
		self.assertEqual(len(provider.get_records("S3-svc.example.com", "A", "52.95.110.1")), 1)


class TestDnsRecords(UnitTestCase):
	def test_names_values_and_health_checks_that_tenant_zero_cannot_set_are_refused(self) -> None:
		provider = _dns_provider_with_records()
		for name, record_type, values, health_check in (
			("example.com", "TXT", ["token"], None),
			("proxy.example.com", "A", ["52.95.110.1"], None),
			("_acme-challenge.example.com", "TXT", ["token"], None),
			("a.b.s3-svc.example.com", "TXT", ["token"], None),
			("s3-svc.example.com.evil.org", "TXT", ["token"], None),
			("_acme-challenge.s3-svc.example.com", "A", ["52.95.110.1"], None),
			("s3-svc.example.com", "CNAME", ["vm-1.example.net"], None),
			("gateway-1.s3-svc.example.com", "CNAME", ["a.example.net", "b.example.net"], None),
			("gateway-1.s3-svc.example.com", "CNAME", ["not a host"], None),
			("_acme-challenge.s3-svc.example.com", "TXT", ['"quoted"'], None),
			("s3-svc.example.com", "A", ["10.0.0.1"], None),
			("s3-svc.example.com", "A", ["52.95.110.1", "52.95.110.1"], None),
			("s3-svc.example.com", "AAAA", ["52.95.110.1"], None),
			("internal-s3-svc.example.com", "AAAA", ["2600:1f18::7"], None),
			("internal-s3-svc.example.com", "AAAA", ["fdaa:1::7"], CHECK),
			("s3-svc.example.com", "A", ["52.95.110.1"], HealthCheck("s3-admin-svc.example.com", "health")),
		):
			with self.subTest(name=name, values=values), self.assertRaises(AtlasUserError):
				provider.upsert_dns_records(name, record_type, values, 300, health_check)

		provider.upsert_records.assert_not_called()

	def test_plain_addresses_replace_the_other_family_and_the_checked_records(self) -> None:
		checked = _member("52.95.110.1", "health-1")
		plain_ipv6 = DnsRecord(NAME, "AAAA", ["2600:1f18::7"])
		plain = DnsRecord(NAME, "A", ["52.95.110.1", "52.95.110.2"], 300)
		provider = _dns_provider_with_records([checked, plain_ipv6], [plain])

		provider.upsert_dns_records(NAME, "A", ["52.95.110.1", "52.95.110.2"], 300)

		provider.upsert_records.assert_called_once_with([plain], [checked, plain_ipv6])
		provider.remove_health_check.assert_called_once_with("health-1")

	def test_checked_addresses_replace_the_plain_record_in_one_batch(self) -> None:
		plain = DnsRecord(NAME, "A", ["52.95.110.1"])
		provider = _dns_provider_with_records([plain], [_member("52.95.110.1", "health-new")])

		provider.upsert_dns_records(NAME, "A", ["52.95.110.1"], 120, CHECK)

		provider.upsert_records.assert_called_once_with([_member("52.95.110.1", "health-new")], [plain])
		provider.remove_health_check.assert_not_called()

	def test_an_unchanged_check_stays_and_the_new_ttl_applies(self) -> None:
		member = _member("52.95.110.1", "health-1")
		provider = _dns_provider_with_records([member], [_member("52.95.110.1", "health-1", ttl=300)])
		provider.get_health_check.return_value = CHECK

		provider.upsert_dns_records(NAME, "A", ["52.95.110.1"], 300, CHECK)

		provider.create_health_check.assert_not_called()
		provider.upsert_records.assert_called_once_with([_member("52.95.110.1", "health-1", ttl=300)], [])
		provider.remove_health_check.assert_not_called()

	def test_a_changed_check_replaces_the_old_check(self) -> None:
		provider = _dns_provider_with_records(
			[_member("52.95.110.1", "health-1")], [_member("52.95.110.1", "health-new")]
		)
		provider.get_health_check.return_value = HealthCheck("s3-admin-svc.example.com", "/ready")

		provider.upsert_dns_records(NAME, "A", ["52.95.110.1"], 120, CHECK)

		provider.upsert_records.assert_called_once_with([_member("52.95.110.1", "health-new")], [])
		provider.remove_health_check.assert_called_once_with("health-1")

	def test_a_rejected_batch_removes_the_new_checks(self) -> None:
		provider = _dns_provider_with_records([], [])
		provider.upsert_records.side_effect = Route53Error("Throttling")

		with self.assertRaises(Route53Error):
			provider.upsert_dns_records(NAME, "A", ["52.95.110.1"], 120, CHECK)

		provider.remove_health_check.assert_called_once_with("health-new")

	def test_a_batch_that_reached_route53_keeps_its_checks(self) -> None:
		provider = _dns_provider_with_records([], [_member("52.95.110.1", "health-new")])
		provider.upsert_records.side_effect = Route53Error("Read timeout")

		with self.assertRaises(Route53Error):
			provider.upsert_dns_records(NAME, "A", ["52.95.110.1"], 120, CHECK)

		provider.remove_health_check.assert_not_called()

	def test_a_cname_replaces_the_txt_record_of_the_name(self) -> None:
		txt = DnsRecord("gateway-1.s3-svc.example.com", "TXT", ["token"])
		provider = _dns_provider_with_records([txt])

		provider.upsert_dns_records("gateway-1.s3-svc.example.com", "CNAME", ["vm-1.example.net"], 300)

		provider.upsert_records.assert_called_once_with(
			[DnsRecord("gateway-1.s3-svc.example.com", "CNAME", ["vm-1.example.net"], 300)], [txt]
		)

	def test_removal_takes_every_record_and_its_check(self) -> None:
		records = [_member("52.95.110.1", "health-1"), DnsRecord(NAME, "TXT", ["token"])]
		provider = _dns_provider_with_records(records, [])

		provider.remove_dns_records(NAME)

		provider.remove_records.assert_called_once_with(records)
		provider.remove_health_check.assert_called_once_with("health-1")

	def test_a_removal_that_reached_route53_still_removes_its_check(self) -> None:
		provider = _dns_provider_with_records([_member("52.95.110.1", "health-1")], [])
		provider.remove_records.side_effect = Route53Error("Read timeout")

		with self.assertRaises(Route53Error):
			provider.remove_dns_records(NAME)

		provider.remove_health_check.assert_called_once_with("health-1")


NAME = "s3-svc.example.com"
CHECK = HealthCheck("s3-admin-svc.example.com", "/health")


def _member(address: str, health_check_id: str, ttl: int = 120) -> DnsRecord:
	return DnsRecord(NAME, "A", [address], ttl, address, health_check_id)


def _dns_provider_with_records(*reads: list[DnsRecord]) -> Route53Provider:
	"""Each call to `get_records` returns the next list: the records before the change, then after it."""
	provider = _build_provider()
	provider.get_records = MagicMock(side_effect=list(reads))
	provider.get_health_check = MagicMock()
	provider.create_health_check = MagicMock(return_value="health-new")
	for method in ("upsert_records", "remove_records", "remove_health_check"):
		setattr(provider, method, MagicMock())
	return provider
