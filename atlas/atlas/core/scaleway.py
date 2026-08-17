"""Tiny Scaleway API client.

Only the endpoints Atlas needs for an Elastic Metal (bare-metal) provider:

Bare metal (zone-scoped, /baremetal/v1/zones/{zone}/...):
- GET    /offers                       — list commercial offers (sizes + pricing)
- GET    /os                           — list installable OS images
- POST   /servers                      — create a server (async; inline `install`)
- GET    /servers/{id}                 — poll status
- DELETE /servers/{id}                 — release the server
- GET    /servers?tags=...             — list by tag, used by the e2e pre-sweep

Account / IAM (global, region-less):
- GET    /account/v3/projects          — credential check + account label
- POST   /iam/v1alpha1/ssh-keys        — register an SSH key (returns its id)
- GET    /iam/v1alpha1/ssh-keys        — find an already-registered key

Flexible IP (zone-scoped, /flexible-ip/v1alpha1/zones/{zone}/...) — the
inbound-v4 primitive, Scaleway's analog of a DigitalOcean reserved IP:
- POST   /fips                         — allocate a flexible IP
- GET    /fips                         — list (discover/import)
- GET    /fips/{id}                    — read one
- POST   /fips/attach                  — bind to a server
- POST   /fips/detach                  — unbind
- DELETE /fips/{id}                    — release

Auth is one static header — `X-Auth-Token: <secret_key>` — on every call (the
IAM API key's Secret Key, not the Access Key, no per-request signing). Same
one-shot, fail-loud shape as the DigitalOcean client: no retry on transient
5xx in this iteration; the operator retries.
"""

from __future__ import annotations

import ipaddress
import time

import requests

DEFAULT_BASE_URL = "https://api.scaleway.com"
DEFAULT_TIMEOUT = 30

# The fixed on-link gateway Scaleway routes every flexible IP through (distinct
# from the server's own subnet gateway). Recorded here for the provider /
# host-NAT layer; the client itself never needs it.
FLEXIBLE_IP_GATEWAY = "62.210.0.1"

# Terminal server states: describe() raises on these so the worker marks the
# Server Broken immediately rather than waiting out the ready timeout.
TERMINAL_SERVER_STATUSES = ("error", "out_of_stock", "locked")


class ScalewayError(Exception):
	pass


class ScalewayClient:
	def __init__(self, secret_key: str, zone: str, base_url: str = DEFAULT_BASE_URL):
		self.secret_key = secret_key
		self.zone = zone
		self.base_url = base_url.rstrip("/")

	# --- Account / IAM (global) ------------------------------------------

	def list_projects(self, organization_id: str | None = None) -> list[dict]:
		path = "/account/v3/projects"
		if organization_id:
			path = f"{path}?organization_id={organization_id}"
		return self._request("GET", path).get("projects", [])

	def verify_credentials(self, organization_id: str | None = None) -> dict:
		"""Probe the credentials and return an account label.

		Lists projects (the cheapest authenticated, region-less call) and returns
		the first project's name as the label. Raises ScalewayError on non-2xx so
		the caller can render a red indicator on failure — mirrors the
		DigitalOcean client's verify_credentials shape (sans rate-limit headers,
		which Scaleway does not document)."""
		path = "/account/v3/projects"
		if organization_id:
			path = f"{path}?organization_id={organization_id}"
		response = self._raw_request("GET", path)
		if response.status_code >= 400:
			raise ScalewayError(f"GET {path} -> {response.status_code}: {response.text}")
		body = response.json()
		projects = body.get("projects", [])
		label = projects[0].get("name") if projects else None
		return {"account_label": label, "project_count": body.get("total_count")}

	def register_ssh_key(self, name: str, public_key: str, project_id: str) -> dict:
		"""Register an SSH public key with IAM and return the created key (with
		its `id`, the handle install referencing). Global, project-scoped.

		IAM v1alpha1 returns the created SSH-key resource at the TOP LEVEL (the
		`id`/`public_key`/`name` fields are unwrapped), NOT inside an `{"ssh_key":
		{...}}` envelope — confirmed against the live API, where assuming the
		envelope raised `KeyError: 'ssh_key'`. Tolerate both shapes (unwrap a legacy
		envelope if one ever appears) so the handler survives an API-shape change."""
		body = {"name": name, "public_key": public_key, "project_id": project_id}
		response = self._request("POST", "/iam/v1alpha1/ssh-keys", json=body)
		return response.get("ssh_key", response)

	def list_ssh_keys(self, project_id: str) -> list[dict]:
		"""List the project's registered SSH keys (first page), so the provider
		can reuse one matched by fingerprint instead of re-registering."""
		return self._request("GET", f"/iam/v1alpha1/ssh-keys?project_id={project_id}").get("ssh_keys", [])

	# --- Bare metal (zone-scoped) ----------------------------------------

	def list_offers(self, subscription_period: str | None = None) -> list[dict]:
		"""List commercial offers in the configured zone. Pagination is not worth
		the round-trips — the catalog is a few dozen offers. `subscription_period`
		(hourly/monthly) is filtered client-side (the raw API takes only
		page/page_size); hourly and monthly are distinct offer ids."""
		offers = self._request("GET", f"{self._bm()}/offers?page_size=100").get("offers", [])
		if subscription_period:
			offers = [o for o in offers if o.get("subscription_period") == subscription_period]
		return offers

	def list_os(self) -> list[dict]:
		"""List installable OS images in the configured zone."""
		return self._request("GET", f"{self._bm()}/os?page_size=100").get("os", [])

	def get_default_partitioning_schema(self, offer_id: str, os_id: str) -> dict:
		"""The vendor's default `partitioning_schema` for an offer+OS pair, returned
		as the bare `{disks, raids, filesystems, zfs}` object the `install` sub-object
		takes. This is the authoritative source for the box's real device names (which
		vary by hardware), so Atlas fetches it and mutates it rather than guessing the
		layout (see `_build_raid_partitioning_schema`). Available on offers/OSs that
		support custom partitioning; raises ScalewayError otherwise."""
		path = f"{self._bm()}/partitioning-schemas/default?offer_id={offer_id}&os_id={os_id}"
		return self._request("GET", path)

	def create_server(
		self,
		*,
		name: str,
		offer_id: str,
		project_id: str,
		tags: list[str],
		install: dict | None = None,
		user_data: str | None = None,
	) -> dict:
		"""Create a server (async). Returns immediately with status='delivering';
		`install` (os_id/hostname/ssh_key_ids[/user/.../partitioning_schema]) is
		the inline one-call deploy. `user_data`, `option_ids`, `protected` are
		TOP-LEVEL, not inside `install`."""
		body: dict = {
			"name": name,
			"offer_id": offer_id,
			"project_id": project_id,
			"tags": tags,
		}
		if install is not None:
			body["install"] = install
		if user_data is not None:
			body["user_data"] = user_data
		return self._request("POST", f"{self._bm()}/servers", json=body)

	def get_server(self, server_id: str) -> dict:
		return self._request("GET", f"{self._bm()}/servers/{server_id}")

	def delete_server(self, server_id: str) -> None:
		self._request("DELETE", f"{self._bm()}/servers/{server_id}", allow_404=True)

	def list_servers_by_tag(self, tag: str) -> list[dict]:
		return self._request("GET", f"{self._bm()}/servers?tags={tag}").get("servers", [])

	def list_servers(self) -> list[dict]:
		"""Every Elastic Metal server in the configured zone (first page, page_size
		100) — unfiltered, for discover/import. The e2e pre-sweep uses the
		tag-filtered sibling; discovery wants untagged, externally-built boxes too.
		The account holds a handful of hosts per zone, so one page is enough; a
		full page is logged so a (rare) overflow announces itself rather than
		silently implying "this is everything"."""
		servers = self._request("GET", f"{self._bm()}/servers?page_size=100").get("servers", [])
		if len(servers) >= 100:
			import frappe

			frappe.logger("atlas").warning(
				f"Scaleway list_servers hit the {len(servers)}-row page cap in zone {self.zone}; "
				"some servers may be missing from discovery"
			)
		return servers

	# --- Flexible IP (zone-scoped) ---------------------------------------

	def create_flexible_ip(self, *, project_id: str, is_ipv6: bool = False) -> dict:
		"""Allocate a flexible IP in the configured zone (unattached). `is_ipv6`
		true reserves a routed /64 block; false (default) a single public /32 v4."""
		body = {"project_id": project_id, "is_ipv6": is_ipv6}
		return self._request("POST", f"{self._fip()}/fips", json=body)

	def get_flexible_ip(self, fip_id: str) -> dict:
		return self._request("GET", f"{self._fip()}/fips/{fip_id}")

	def list_flexible_ips(self) -> list[dict]:
		"""List the account's flexible IPs in the zone (first page)."""
		return self._request("GET", f"{self._fip()}/fips?page_size=100").get("flexible_ips", [])

	def attach_flexible_ip(self, fip_id: str, server_id: str) -> dict:
		"""Bind a flexible IP to a server. Same-AZ constraint (we are single-zone).
		The packet then arrives at the server with destination = the flexible IP
		itself, routed via the fixed gateway — the host 1:1-NATs it to the guest."""
		body = {"fips_ids": [fip_id], "server_id": server_id}
		return self._request("POST", f"{self._fip()}/fips/attach", json=body)

	def detach_flexible_ip(self, fip_id: str) -> dict:
		"""Unbind a flexible IP from whatever server holds it. Asynchronous — the
		FIP transits `detaching`; wait for it to settle so a delete or re-attach
		issued immediately after isn't rejected (mirrors the DO unassign wait)."""
		body = {"fips_ids": [fip_id]}
		action = self._request("POST", f"{self._fip()}/fips/detach", json=body)
		self._wait_flexible_ip_detached(fip_id)
		return action

	def delete_flexible_ip(self, fip_id: str) -> None:
		self._request("DELETE", f"{self._fip()}/fips/{fip_id}", allow_404=True)

	def _wait_flexible_ip_detached(self, fip_id: str, timeout_seconds: int = 60) -> None:
		"""Poll until the flexible IP is no longer bound to a server. Tolerates a
		404 (already gone). Raises if still attached past the timeout — a stuck
		detach is a real failure, not something to swallow."""
		deadline = time.monotonic() + timeout_seconds
		while True:
			try:
				fip = self.get_flexible_ip(fip_id)
			except ScalewayError as error:
				if "404" in str(error):
					return
				raise
			if not fip.get("server_id") and fip.get("status") != "detaching":
				return
			if time.monotonic() >= deadline:
				raise ScalewayError(f"flexible IP {fip_id} still attached after {timeout_seconds}s")
			time.sleep(2)

	# --- internals -------------------------------------------------------

	def _bm(self) -> str:
		return f"/baremetal/v1/zones/{self.zone}"

	def _fip(self) -> str:
		return f"/flexible-ip/v1alpha1/zones/{self.zone}"

	def _request(self, method: str, path: str, json: dict | None = None, allow_404: bool = False):
		response = self._raw_request(method, path, json=json)
		if response.status_code == 204:
			return {}
		if response.status_code == 404 and allow_404:
			return {}
		if response.status_code >= 400:
			raise ScalewayError(f"{method} {path} -> {response.status_code}: {response.text}")
		if not response.content:
			return {}
		return response.json()

	def _raw_request(self, method: str, path: str, json: dict | None = None) -> "requests.Response":
		"""HTTP call returning the full Response so callers can read the status
		themselves (verify_credentials). Status handling otherwise lives in
		`_request`."""
		url = f"{self.base_url}{path}"
		headers = {
			"X-Auth-Token": self.secret_key,
			"Content-Type": "application/json",
			"Accept": "application/json",
		}
		return requests.request(method, url, json=json, headers=headers, timeout=DEFAULT_TIMEOUT)


def public_ipv4(server: dict) -> str:
	"""The server's public IPv4 (the bundled primary v4) from its `ips[]`."""
	for entry in server.get("ips", []):
		if entry.get("version") == "IPv4" or entry.get("version") == 4:
			return entry["address"]
	raise ScalewayError(f"Server {server.get('id')} has no IPv4")


def public_ipv6(server: dict) -> tuple[str, str]:
	"""Return (host_address, prefix_cidr) for the server's public IPv6.

	Scaleway routes a /64 to the host; unlike DigitalOcean there is no edge
	/124 limit, so the whole /64 is the VM range (see networking spec). The IP
	entry may carry an explicit prefix length (`prefix_length` / a `/N` suffix
	on the address); default to /64. Raises if the server has no public v6.
	"""
	for entry in server.get("ips", []):
		if entry.get("version") == "IPv6" or entry.get("version") == 6:
			address = entry["address"]
			# Address may arrive bare or as "<addr>/<len>"; the entry may also
			# carry prefix_length / netmask. Default to /64.
			prefix_length = entry.get("prefix_length") or entry.get("netmask") or 64
			if "/" in address:
				address, _, suffix = address.partition("/")
				prefix_length = int(suffix)
			return address, _network_cidr(address, int(prefix_length))
	raise ScalewayError(f"Server {server.get('id')} has no IPv6")


def flexible_ip_address(fip: dict) -> str:
	"""The bare address of a flexible IP (its `ip_address` may carry a /32 or
	/64 suffix)."""
	address = fip.get("ip_address") or fip.get("address") or ""
	return address.partition("/")[0] if address else address


def flexible_ip_server_id(fip: dict) -> str | None:
	"""The server id a flexible IP is attached to, or None if floating."""
	return fip.get("server_id") or None


def _network_cidr(address: str, prefix_length: int) -> str:
	network = ipaddress.IPv6Network(f"{address}/{prefix_length}", strict=False)
	return str(network)
