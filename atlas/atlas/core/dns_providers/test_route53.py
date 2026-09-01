from __future__ import annotations

from unittest.mock import MagicMock

from frappe.tests import UnitTestCase

from atlas.atlas.core.dns_providers.route53 import Route53Provider


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
