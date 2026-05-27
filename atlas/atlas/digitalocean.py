"""Tiny DigitalOcean API client.

Only the endpoints Atlas needs:

- GET  /account                  — credential check
- POST /droplets                 — create
- GET  /droplets/{id}            — poll
- DELETE /droplets/{id}          — delete
- GET  /droplets?tag_name=...    — list by tag, used by the e2e pre-sweep

No retry on transient 5xx in this iteration. One shot, fail loud. Operator
retries.
"""

import ipaddress
import time

import requests

DEFAULT_BASE_URL = "https://api.digitalocean.com/v2"
DEFAULT_TIMEOUT = 30
ACTIVE_POLL_INTERVAL = 5
DEFAULT_ACTIVE_TIMEOUT = 300


class DigitalOceanError(Exception):
	pass


class DigitalOceanClient:
	def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL):
		self.token = token
		self.base_url = base_url.rstrip("/")

	def account(self) -> dict:
		return self._request("GET", "/account")["account"]

	def verify_credentials(self) -> dict:
		"""Like account(), but also returns the rate-limit headers DO sets on
		every response. The Server Provider form surfaces them as
		"4998 / 5000 remaining" so the operator can see token health at a
		glance without opening the network tab. Raises DigitalOceanError on
		non-2xx so the caller can render a red indicator on failure.
		"""
		response = self._raw_request("GET", "/account")
		if response.status_code >= 400:
			raise DigitalOceanError(
				f"GET /account -> {response.status_code}: {response.text}"
			)
		body = response.json()
		return {
			"email": body.get("account", {}).get("email"),
			"rate_limit": int(response.headers["RateLimit-Limit"])
				if "RateLimit-Limit" in response.headers else None,
			"rate_remaining": int(response.headers["RateLimit-Remaining"])
				if "RateLimit-Remaining" in response.headers else None,
		}

	def create_droplet(
		self,
		*,
		name: str,
		region: str,
		size: str,
		image: str,
		ssh_key_ids: list[str],
		tags: list[str],
		ipv6: bool = True,
	) -> dict:
		body = {
			"name": name,
			"region": region,
			"size": size,
			"image": image,
			"ssh_keys": ssh_key_ids,
			"ipv6": ipv6,
			"tags": tags,
		}
		return self._request("POST", "/droplets", json=body)["droplet"]

	def get_droplet(self, droplet_id: int) -> dict:
		return self._request("GET", f"/droplets/{droplet_id}")["droplet"]

	def wait_for_active(self, droplet_id: int, timeout_seconds: int = DEFAULT_ACTIVE_TIMEOUT) -> dict:
		deadline = time.monotonic() + timeout_seconds
		while True:
			droplet = self.get_droplet(droplet_id)
			if droplet.get("status") == "active":
				return droplet
			if time.monotonic() >= deadline:
				raise DigitalOceanError(
					f"Droplet {droplet_id} not active after {timeout_seconds}s "
					f"(status={droplet.get('status')})"
				)
			time.sleep(ACTIVE_POLL_INTERVAL)

	def delete_droplet(self, droplet_id: int) -> None:
		self._request("DELETE", f"/droplets/{droplet_id}", allow_404=True)

	def list_droplets_by_tag(self, tag: str) -> list[dict]:
		return self._request("GET", f"/droplets?tag_name={tag}").get("droplets", [])

	def _request(self, method: str, path: str, json: dict | None = None, allow_404: bool = False):
		response = self._raw_request(method, path, json=json)
		if response.status_code == 204:
			return {}
		if response.status_code == 404 and allow_404:
			return {}
		if response.status_code >= 400:
			raise DigitalOceanError(
				f"{method} {path} -> {response.status_code}: {response.text}"
			)
		if not response.content:
			return {}
		return response.json()

	def _raw_request(self, method: str, path: str, json: dict | None = None) -> "requests.Response":
		"""HTTP call that returns the full Response so callers can read
		headers (rate-limit, ETag, etc.). Status handling lives in
		`_request`; callers that bypass `_request` (verify_credentials)
		check the status themselves."""
		url = f"{self.base_url}{path}"
		headers = {
			"Authorization": f"Bearer {self.token}",
			"Content-Type": "application/json",
			"Accept": "application/json",
		}
		return requests.request(
			method,
			url,
			json=json,
			headers=headers,
			timeout=DEFAULT_TIMEOUT,
		)


def public_ipv6(droplet: dict) -> tuple[str, str]:
	"""Return (host_address, prefix_cidr) for the droplet's public IPv6.

	Raises DigitalOceanError if the droplet has no public v6.
	"""
	for entry in droplet.get("networks", {}).get("v6", []):
		if entry.get("type") == "public":
			address = entry["ip_address"]
			prefix_length = entry.get("netmask", 64)
			return address, _network_cidr(address, prefix_length)
	raise DigitalOceanError(f"Droplet {droplet.get('id')} has no public IPv6")


def public_ipv4(droplet: dict) -> str:
	for entry in droplet.get("networks", {}).get("v4", []):
		if entry.get("type") == "public":
			return entry["ip_address"]
	raise DigitalOceanError(f"Droplet {droplet.get('id')} has no public IPv4")


def _network_cidr(address: str, prefix_length: int) -> str:
	network = ipaddress.IPv6Network(f"{address}/{prefix_length}", strict=False)
	return str(network)
