"""Route 53 DNS provider — DNS-01 via AWS Route 53.

Reads `Route53 Settings` for the IAM credentials (the secret via
`atlas.atlas.core.secrets.get_secret`, mirroring how `DigitalOceanProvider` reads its
token). The actual TXT-record dance is certbot's `dns-route53` plugin's job; this
class only supplies the plugin flag and the AWS credential env. `authenticate()`
proves the credentials reach the account by listing hosted zones — the lightest
read that exercises the same `route53:*` permissions issuance needs.
"""

from __future__ import annotations

import frappe

from atlas.atlas.core.secrets import get_secret
from atlas.atlas.services.dns import register
from atlas.atlas.services.dns.base import AuthResult, DnsProvider, WildcardTargets

# Round-robin A/AAAA TTL. Short so a proxy rebuild (new /128) or reserved-IP
# reattach propagates quickly — the records are reconciled, not set-and-forget.
WILDCARD_TTL_SECONDS = 60


@register
class Route53DnsProvider(DnsProvider):
	provider_type = "Route53"

	def __init__(self) -> None:
		settings = frappe.get_single("Route53 Settings")
		self.access_key_id = settings.access_key_id
		self.secret_access_key = get_secret("Route53 Settings", "Route53 Settings", "secret_access_key")
		self.region = settings.region or "us-east-1"

	def _client(self):
		"""A boto3 route53 client from the configured creds. Import is local so the
		controller-only boto3 dependency never loads at module import (the registry
		imports this module on every `for_domain_provider`)."""
		import boto3

		return boto3.client(
			"route53",
			aws_access_key_id=self.access_key_id,
			aws_secret_access_key=self.secret_access_key,
			region_name=self.region,
		)

	def authenticate(self) -> AuthResult:
		try:
			client = self._client()
		except ImportError:
			return AuthResult(ok=False, error="boto3 not installed on the controller")
		try:
			response = client.list_hosted_zones(MaxItems="1")
		except Exception as exception:
			return AuthResult(ok=False, error=str(exception))
		zones = response.get("HostedZones") or []
		label = zones[0]["Name"].rstrip(".") if zones else "no hosted zones"
		return AuthResult(ok=True, account_label=label)

	def upsert_wildcard(self, domain: str, targets: WildcardTargets) -> list[str]:
		client = self._client()
		zone_id = self._hosted_zone_id(client, domain)
		record_name = f"*.{domain}"
		changes = []
		for record_type, values in (("A", targets.ipv4), ("AAAA", targets.ipv6)):
			if not values:
				# Never publish a wildcard pointing at nothing; leave any existing
				# record of this type untouched (a half-empty fleet shouldn't blackhole).
				continue
			changes.append(
				{
					"Action": "UPSERT",
					"ResourceRecordSet": {
						"Name": record_name,
						"Type": record_type,
						"TTL": WILDCARD_TTL_SECONDS,
						"ResourceRecords": [{"Value": value} for value in values],
					},
				}
			)
		if not changes:
			frappe.throw(f"upsert_wildcard for {record_name}: no proxy addresses to publish")
		client.change_resource_record_sets(
			HostedZoneId=zone_id,
			ChangeBatch={
				"Comment": f"Atlas regional wildcard for {domain}",
				"Changes": changes,
			},
		)
		return [f"{change['ResourceRecordSet']['Type']} {record_name}" for change in changes]

	def _hosted_zone_id(self, client, domain: str) -> str:
		"""The hosted zone that owns `domain`, found by walking up the name — the
		same discovery `certbot-dns-route53` does (zone may be `<domain>` or any
		parent, e.g. `atlas1.x.frappe.dev` resolves to the `x.frappe.dev` zone).
		Picks the LONGEST matching zone suffix (the most specific zone wins)."""
		paginator = client.get_paginator("list_hosted_zones")
		best: tuple[int, str] | None = None
		for page in paginator.paginate():
			for zone in page.get("HostedZones", []):
				if zone.get("Config", {}).get("PrivateZone"):
					continue
				zone_name = zone["Name"].rstrip(".")
				if domain == zone_name or domain.endswith("." + zone_name):
					if best is None or len(zone_name) > best[0]:
						best = (len(zone_name), zone["Id"])
		if best is None:
			frappe.throw(f"no Route 53 hosted zone found for {domain!r}")
		return best[1]

	def credential_env(self) -> dict[str, str]:
		return {
			"AWS_ACCESS_KEY_ID": self.access_key_id,
			"AWS_SECRET_ACCESS_KEY": self.secret_access_key,
			"AWS_DEFAULT_REGION": self.region,
		}

	def certbot_authenticator(self) -> str:
		# `certbot-dns-route53` discovers the hosted zone from the domain name at
		# issue time, so no zone-id is needed — just name the authenticator. The
		# script renders this as `--dns-route53`.
		return "route53"
