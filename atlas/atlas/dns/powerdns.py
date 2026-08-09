"""PowerDNS DNS provider — wildcard publishing through the Authoritative HTTP API.

PowerDNS uses a small REST API authenticated by the `X-API-Key` header. Atlas
only needs three endpoints here:

- `GET /api/v1/servers/{server_id}` for the settings Test Connection button.
- `GET /api/v1/servers/{server_id}/zones?zone=<name>` while finding the zone
  that owns a Root Domain.
- `PATCH /api/v1/servers/{server_id}/zones/{zone_id}` to replace wildcard
  A/AAAA RRsets.
"""

from __future__ import annotations

import os
from urllib.parse import quote

import frappe
import requests

from atlas.atlas.dns import register
from atlas.atlas.dns.base import AuthResult, DnsProvider, WildcardTargets
from atlas.atlas.secrets import get_secret

WILDCARD_TTL_SECONDS = 60


class PowerDNSError(Exception):
	pass


@register
class PowerDNSDnsProvider(DnsProvider):
	provider_type = "PowerDNS"

	def __init__(self) -> None:
		settings = frappe.get_single("PowerDNS Settings")
		self.api_url = (settings.api_url or "").rstrip("/")
		self.api_key = get_secret("PowerDNS Settings", "PowerDNS Settings", "api_key")
		self.server_id = settings.server_id or "localhost"

	def authenticate(self) -> AuthResult:
		try:
			server = self._request("GET", f"/servers/{quote(self.server_id, safe='')}")
		except Exception as exception:
			return AuthResult(ok=False, error=str(exception))
		label = server.get("id") or self.server_id
		version = server.get("version")
		if version:
			label = f"{label} ({version})"
		return AuthResult(ok=True, account_label=label)

	def upsert_wildcard(self, domain: str, targets: WildcardTargets) -> list[str]:
		zone_id = self._zone_id(domain)
		record_name = f"*.{domain}."
		rrsets = []
		for record_type, values in (("A", targets.ipv4), ("AAAA", targets.ipv6)):
			if not values:
				continue
			rrsets.append(
				{
					"name": record_name,
					"type": record_type,
					"ttl": WILDCARD_TTL_SECONDS,
					"changetype": "REPLACE",
					"records": [{"content": value, "disabled": False} for value in values],
				}
			)
		if not rrsets:
			frappe.throw(f"upsert_wildcard for {record_name}: no proxy addresses to publish")
		self._request(
			"PATCH",
			f"/servers/{quote(self.server_id, safe='')}/zones/{quote(zone_id, safe='')}",
			json={"rrsets": rrsets},
		)
		return [f"{rrset['type']} {record_name.rstrip('.')}" for rrset in rrsets]

	def _zone_id(self, domain: str) -> str:
		for candidate in _candidate_zones(domain):
			zones = self._request(
				"GET",
				f"/servers/{quote(self.server_id, safe='')}/zones",
				params={"zone": f"{candidate}."},
			)
			if zones:
				return zones[0].get("id") or zones[0]["name"]
		frappe.throw(f"no PowerDNS zone found for {domain!r}")

	def _request(self, method: str, path: str, **kwargs):
		if not self.api_url:
			raise PowerDNSError("PowerDNS Settings.api_url is required")
		response = requests.request(
			method,
			f"{self.api_url}/api/v1{path}",
			headers={
				"X-API-Key": self.api_key,
				"Accept": "application/json",
			},
			timeout=30,
			**kwargs,
		)
		if response.status_code >= 400:
			message = response.text
			try:
				payload = response.json()
				message = payload.get("error") or message
			except ValueError:
				pass
			raise PowerDNSError(f"{method} {path} -> {response.status_code}: {message}")
		if response.status_code == 204 or not response.content:
			return {}
		return response.json()

	def credential_env(self) -> dict[str, str]:
		return {
			"POWERDNS_API_URL": self.api_url,
			"POWERDNS_API_KEY": self.api_key,
			"POWERDNS_SERVER_ID": self.server_id,
		}

	def certbot_authenticator(self) -> str:
		return "powerdns"

	def certbot_args(self, domain: str) -> list[str]:
		return [
			"--authenticator",
			"dns-pdns",
			"--dns-pdns-credentials",
			_powerdns_credentials_path(domain),
		]


def _candidate_zones(domain: str) -> list[str]:
	labels = domain.rstrip(".").split(".")
	return [".".join(labels[index:]) for index in range(len(labels) - 1)]


def _powerdns_credentials_path(domain: str) -> str:
	return os.path.abspath(os.path.join(os.path.expanduser("~"), ".atlas", "certbot", domain, "powerdns.ini"))
