"""Tiny DigitalOcean API client.

Only the endpoints Atlas needs:

- GET  /account                       — credential check
- POST /droplets                      — create
- GET  /droplets/{id}                 — poll
- DELETE /droplets/{id}               — delete
- GET  /droplets?tag_name=...         — list by tag, used by the e2e pre-sweep
- POST /reserved_ips                  — allocate a reserved IP (to a region)
- GET  /reserved_ips                  — list reserved IPs (discover/import)
- GET  /reserved_ips/{ip}             — read one
- POST /reserved_ips/{ip}/actions     — assign/unassign to a droplet
- DELETE /reserved_ips/{ip}           — release

No retry on transient 5xx in this iteration. One shot, fail loud. Operator
retries.
"""

import ipaddress

import requests

DEFAULT_BASE_URL = "https://api.digitalocean.com/v2"
DEFAULT_TIMEOUT = 30


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
		every response. The DigitalOcean Settings form surfaces them as
		"4998 / 5000 remaining" so the operator can see token health at a
		glance without opening the network tab. Raises DigitalOceanError on
		non-2xx so the caller can render a red indicator on failure.
		"""
		response = self._raw_request("GET", "/account")
		if response.status_code >= 400:
			raise DigitalOceanError(f"GET /account -> {response.status_code}: {response.text}")
		body = response.json()
		return {
			"email": body.get("account", {}).get("email"),
			"rate_limit": int(response.headers["RateLimit-Limit"])
			if "RateLimit-Limit" in response.headers
			else None,
			"rate_remaining": int(response.headers["RateLimit-Remaining"])
			if "RateLimit-Remaining" in response.headers
			else None,
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

	def delete_droplet(self, droplet_id: int) -> None:
		self._request("DELETE", f"/droplets/{droplet_id}", allow_404=True)

	def list_droplets_by_tag(self, tag: str) -> list[dict]:
		return self._request("GET", f"/droplets?tag_name={tag}").get("droplets", [])

	def list_droplets(self) -> list[dict]:
		"""Every droplet in the account (first page, per_page 200) — unfiltered,
		for discover/import. The e2e pre-sweep uses the tag-filtered sibling;
		discovery wants untagged, externally-built droplets too. The account holds
		a handful, so one page is enough; a full page is logged so a (rare)
		overflow announces itself rather than silently implying "this is
		everything"."""
		droplets = self._request("GET", "/droplets?per_page=200").get("droplets", [])
		if len(droplets) >= 200:
			import frappe

			frappe.logger("atlas").warning(
				f"DigitalOcean list_droplets hit the {len(droplets)}-row page cap; "
				"some droplets may be missing from discovery"
			)
		return droplets

	def create_reserved_ip(self, region: str) -> dict:
		"""Allocate a reserved IP to a region (not yet assigned to a droplet).

		DO also accepts `{"droplet_id": N}` to allocate-and-assign in one call,
		but we keep the two steps separate (allocate to the Server's region,
		assign on attach) so the Reserved IP row exists before any VM binds it."""
		return self._request("POST", "/reserved_ips", json={"region": region})["reserved_ip"]

	def get_reserved_ip(self, ip: str) -> dict:
		return self._request("GET", f"/reserved_ips/{ip}")["reserved_ip"]

	def list_reserved_ips(self) -> list[dict]:
		"""List the account's reserved IPs (first page). The account holds a
		handful, so pagination is not worth the extra round-trips here."""
		return self._request("GET", "/reserved_ips").get("reserved_ips", [])

	def assign_reserved_ip(self, ip: str, droplet_id: int) -> dict:
		"""Bind the reserved IP to a droplet. The droplet gets the address as an
		anchor IP; the host then 1:1-NATs it to the guest (a later Task)."""
		return self._request(
			"POST",
			f"/reserved_ips/{ip}/actions",
			json={"type": "assign", "droplet_id": droplet_id},
		)["action"]

	def unassign_reserved_ip(self, ip: str) -> dict:
		"""Release the reserved IP from whatever droplet holds it, leaving it
		allocated to the account/region for re-assignment.

		Waits for the unassign action to settle (the IP's `droplet` going null)
		before returning: DO's unassign is asynchronous, and a `delete` or
		re-`assign` issued before it completes is rejected `422 unprocessable`
		("an action is in progress"). Making detach synchronous here removes that
		race for every caller (release, re-attach elsewhere)."""
		action = self._request(
			"POST",
			f"/reserved_ips/{ip}/actions",
			json={"type": "unassign"},
		)["action"]
		self._wait_reserved_ip_unassigned(ip)
		return action

	def _wait_reserved_ip_unassigned(self, ip: str, timeout_seconds: int = 60) -> None:
		"""Poll until the reserved IP is no longer bound to a droplet. Tolerates a
		404 (IP already gone). Raises if it is still assigned past the timeout —
		a stuck unassign is a real failure, not something to swallow."""
		import time

		deadline = time.monotonic() + timeout_seconds
		while True:
			try:
				reserved = self.get_reserved_ip(ip)
			except DigitalOceanError as error:
				if "404" in str(error):
					return
				raise
			if not reserved.get("droplet"):
				return
			if time.monotonic() >= deadline:
				raise DigitalOceanError(f"reserved IP {ip} still assigned after {timeout_seconds}s")
			time.sleep(2)

	def delete_reserved_ip(self, ip: str) -> None:
		self._request("DELETE", f"/reserved_ips/{ip}", allow_404=True)

	def _request(self, method: str, path: str, json: dict | None = None, allow_404: bool = False):
		response = self._raw_request(method, path, json=json)
		if response.status_code == 204:
			return {}
		if response.status_code == 404 and allow_404:
			return {}
		if response.status_code >= 400:
			raise DigitalOceanError(f"{method} {path} -> {response.status_code}: {response.text}")
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


def reserved_ip_droplet_id(reserved_ip: dict) -> int | None:
	"""The droplet id a reserved IP is assigned to, or None if floating.

	DO returns `droplet: null` for an unassigned reserved IP and the embedded
	droplet object once it's bound."""
	droplet = reserved_ip.get("droplet")
	return droplet.get("id") if droplet else None


def _network_cidr(address: str, prefix_length: int) -> str:
	network = ipaddress.IPv6Network(f"{address}/{prefix_length}", strict=False)
	return str(network)
