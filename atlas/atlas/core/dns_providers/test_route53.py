from __future__ import annotations

from ipaddress import ip_address
from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from frappe.tests import UnitTestCase

from atlas.atlas.core.dns_providers.route53 import Route53Provider
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

		health_check_id = provider.create_https_health_check(
			"203.0.113.9", "proxy-001.example.com", "/readyz"
		)

		self.assertEqual(health_check_id, "health-1")
		configuration = provider.client.create_health_check.call_args.kwargs["HealthCheckConfig"]
		self.assertEqual(configuration["IPAddress"], "203.0.113.9")
		self.assertEqual(configuration["FullyQualifiedDomainName"], "proxy-001.example.com")
		self.assertEqual(configuration["ResourcePath"], "/readyz")
		self.assertEqual(configuration["RequestInterval"], 30)
		self.assertEqual(configuration["FailureThreshold"], 2)
		self.assertTrue(configuration["EnableSNI"])

	def test_multivalue_record_attaches_the_health_check(self) -> None:
		provider = _build_provider()

		provider.upsert_multivalue_a_record("proxy.example.com", "proxy-001", "203.0.113.9", "health-1")

		record = provider.client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"][0][
			"ResourceRecordSet"
		]
		self.assertEqual(record["SetIdentifier"], "proxy-001")
		self.assertTrue(record["MultiValueAnswer"])
		self.assertEqual(record["TTL"], 120)
		self.assertEqual(record["HealthCheckId"], "health-1")
		self.assertEqual(record["ResourceRecords"], [{"Value": "203.0.113.9"}])

	def test_multivalue_record_removal_uses_the_current_record(self) -> None:
		provider = _build_provider()
		record = {
			"Name": "proxy.example.com.",
			"Type": "A",
			"SetIdentifier": "proxy-001",
			"MultiValueAnswer": True,
			"TTL": 30,
			"ResourceRecords": [{"Value": "203.0.113.9"}],
			"HealthCheckId": "health-1",
		}
		provider.client.list_resource_record_sets.return_value = {"ResourceRecordSets": [record]}

		provider.remove_multivalue_a_record("proxy.example.com", "proxy-001")

		provider.client.change_resource_record_sets.assert_called_once_with(
			HostedZoneId="Z123",
			ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": record}]},
		)

	def test_upsert_record_sends_upsert_change(self) -> None:
		provider = _build_provider()

		provider.upsert_record("A", "app.example.com", ["1.2.3.4"], ttl=60)

		provider.client.change_resource_record_sets.assert_called_once_with(
			HostedZoneId="Z123",
			ChangeBatch={
				"Changes": [
					{
						"Action": "UPSERT",
						"ResourceRecordSet": {
							"Name": "app.example.com",
							"Type": "A",
							"TTL": 60,
							"ResourceRecords": [{"Value": "1.2.3.4"}],
						},
					}
				]
			},
		)

	def test_upsert_a_record_wraps_upsert_record(self) -> None:
		provider = _build_provider()

		provider.upsert_a_record("app.example.com", "1.2.3.4", ttl=60)

		change = provider.client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"][0]
		self.assertEqual(change["ResourceRecordSet"]["Type"], "A")
		self.assertEqual(change["ResourceRecordSet"]["ResourceRecords"], [{"Value": "1.2.3.4"}])

	def test_upsert_cname_record_wraps_upsert_record(self) -> None:
		provider = _build_provider()

		provider.upsert_cname_record("www.example.com", "app.example.com")

		change = provider.client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"][0]
		self.assertEqual(change["ResourceRecordSet"]["Type"], "CNAME")
		self.assertEqual(change["ResourceRecordSet"]["ResourceRecords"], [{"Value": "app.example.com"}])

	def test_upsert_txt_record_quotes_the_value(self) -> None:
		provider = _build_provider()

		provider.upsert_txt_record("_verify.example.com", "token-123")

		change = provider.client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"][0]
		self.assertEqual(change["ResourceRecordSet"]["Type"], "TXT")
		self.assertEqual(change["ResourceRecordSet"]["ResourceRecords"], [{"Value": '"token-123"'}])

	def test_remove_record_deletes_matching_record(self) -> None:
		provider = _build_provider()
		provider.client.list_resource_record_sets.return_value = {
			"ResourceRecordSets": [
				{
					"Name": "app.example.com.",
					"Type": "A",
					"TTL": 60,
					"ResourceRecords": [{"Value": "1.2.3.4"}],
				}
			]
		}

		provider.remove_record("A", "app.example.com")

		provider.client.change_resource_record_sets.assert_called_once_with(
			HostedZoneId="Z123",
			ChangeBatch={
				"Changes": [
					{
						"Action": "DELETE",
						"ResourceRecordSet": {
							"Name": "app.example.com",
							"Type": "A",
							"TTL": 60,
							"ResourceRecords": [{"Value": "1.2.3.4"}],
						},
					}
				]
			},
		)

	def test_remove_record_is_a_no_op_when_missing(self) -> None:
		provider = _build_provider()
		provider.client.list_resource_record_sets.return_value = {"ResourceRecordSets": []}

		provider.remove_record("A", "app.example.com")

		provider.client.change_resource_record_sets.assert_not_called()

	def test_remove_a_record_wraps_remove_record(self) -> None:
		provider = _build_provider()
		provider.client.list_resource_record_sets.return_value = {
			"ResourceRecordSets": [
				{
					"Name": "app.example.com.",
					"Type": "A",
					"TTL": 60,
					"ResourceRecords": [{"Value": "1.2.3.4"}],
				}
			]
		}

		provider.remove_a_record("app.example.com")

		change = provider.client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"][0]
		self.assertEqual(change["ResourceRecordSet"]["Type"], "A")

	def test_remove_cname_record_wraps_remove_record(self) -> None:
		provider = _build_provider()
		provider.client.list_resource_record_sets.return_value = {
			"ResourceRecordSets": [
				{
					"Name": "www.example.com.",
					"Type": "CNAME",
					"TTL": 60,
					"ResourceRecords": [{"Value": "app.example.com"}],
				}
			]
		}

		provider.remove_cname_record("www.example.com")

		change = provider.client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"][0]
		self.assertEqual(change["ResourceRecordSet"]["Type"], "CNAME")

	def test_remove_txt_record_wraps_remove_record(self) -> None:
		provider = _build_provider()
		provider.client.list_resource_record_sets.return_value = {
			"ResourceRecordSets": [
				{
					"Name": "_verify.example.com.",
					"Type": "TXT",
					"TTL": 60,
					"ResourceRecords": [{"Value": '"token-123"'}],
				}
			]
		}

		provider.remove_txt_record("_verify.example.com")

		change = provider.client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"][0]
		self.assertEqual(change["ResourceRecordSet"]["Type"], "TXT")

	def test_service_records_point_to_public_and_private_addresses(self) -> None:
		provider = _build_provider()

		provider.upsert_service_record("s3-svc.example.com", ip_address("52.95.110.1"), 300)
		provider.upsert_service_record("internal-s3-svc.example.com", ip_address("fdaa:1::7"), 300)

		records = [
			call.kwargs["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]
			for call in provider.client.change_resource_record_sets.call_args_list
		]
		self.assertEqual(
			records,
			[
				{
					"Name": "s3-svc.example.com",
					"Type": "A",
					"TTL": 300,
					"ResourceRecords": [{"Value": "52.95.110.1"}],
				},
				{
					"Name": "internal-s3-svc.example.com",
					"Type": "AAAA",
					"TTL": 300,
					"ResourceRecords": [{"Value": "fdaa:1::7"}],
				},
			],
		)

	def test_service_record_replaces_the_other_address_family_in_one_change(self) -> None:
		provider = _build_provider()
		old_record = {
			"Name": "s3-svc.example.com.",
			"Type": "A",
			"TTL": 300,
			"ResourceRecords": [{"Value": "52.95.110.1"}],
		}
		provider.client.list_resource_record_sets.return_value = {"ResourceRecordSets": [old_record]}

		provider.upsert_service_record("s3-svc.example.com", ip_address("2600:1f18::7"), 60)

		provider.client.change_resource_record_sets.assert_called_once_with(
			HostedZoneId="Z123",
			ChangeBatch={
				"Changes": [
					{
						"Action": "UPSERT",
						"ResourceRecordSet": {
							"Name": "s3-svc.example.com",
							"Type": "AAAA",
							"TTL": 60,
							"ResourceRecords": [{"Value": "2600:1f18::7"}],
						},
					},
					{"Action": "DELETE", "ResourceRecordSet": old_record},
				]
			},
		)

	def test_service_record_refuses_other_names_and_wrong_addresses(self) -> None:
		provider = _build_provider()

		for name, address in (
			("proxy.example.com", "52.95.110.1"),
			("internal-proxy.example.com", "fdaa:1::7"),
			("s3-svc.cargo.example.com", "52.95.110.1"),
			("s3-svc.example.org", "52.95.110.1"),
			("s3-svc.example.com", "fdaa:1::7"),
			("internal-s3-svc.example.com", "52.95.110.1"),
			("s3-svc.example.com", "224.0.0.1"),
			("internal-s3-svc.example.com", "10.0.0.7"),
			("internal-s3-svc.example.com", "::1"),
		):
			with self.subTest(name=name, address=address), self.assertRaises(AtlasUserError):
				provider.upsert_service_record(name, ip_address(address), 300)

		provider.client.change_resource_record_sets.assert_not_called()
