"""TLS Certificate DocType — the issued regional wildcard, and the wiring that
lands it on every proxy VM in the domain's region.

`issue()` (and its idempotent twin `renew()`) drive the domain's TLS provider to
produce PEMs on the controller's disk, record the paths + validity window + status,
then `_push_to_proxies()` reads those PEMs and calls the EXISTING
`atlas.atlas.proxy.push_cert(vm, fullchain, privkey)` for each `is_proxy` VM in the
region — nginx reloads on each guest. This is the producer the proxy design's
`push_cert` was always missing.

The certificate is keyed to a `Root Domain` (one cert per domain == per region).
The provider seam means this controller never branches on Let's Encrypt vs ZeroSSL
vs Self-Managed; it resolves `for_tls_provider` / `for_domain_provider` and calls
`issue()`.
"""

from __future__ import annotations

import pathlib

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas import dns, proxy, tls

# Renew certs whose expiry is within this many days (the scheduled job's window).
RENEWAL_WINDOW_DAYS = 30


class TLSCertificate(Document):
	def before_insert(self) -> None:
		self._derive_common_name()
		self._denormalize_provider()

	def validate(self) -> None:
		self._derive_common_name()

	def _derive_common_name(self) -> None:
		domain = frappe.db.get_value("Root Domain", self.root_domain, "domain")
		if domain:
			self.common_name = f"*.{domain}"

	def _denormalize_provider(self) -> None:
		if not self.tls_provider:
			self.tls_provider = frappe.db.get_value("Root Domain", self.root_domain, "tls_provider")

	# --- Issue / renew ---------------------------------------------------

	@frappe.whitelist()
	def issue(self) -> None:
		"""Run the TLS provider's issue flow, record the result, push to proxies.

		On any failure the cert is flipped to `Failed` and the error re-raised, so a
		broken issuance is visible on the row (mirrors the Task failure model)."""
		self._issue_or_renew()

	@frappe.whitelist()
	def renew(self) -> None:
		"""Idempotent re-issue. Same flow as `issue()` — certbot renews-or-skips —
		named separately so the button and the scheduler read clearly."""
		self._issue_or_renew()

	def _issue_or_renew(self) -> None:
		domain_row = frappe.get_doc("Root Domain", self.root_domain)
		try:
			tls_provider = tls.for_tls_provider(domain_row.tls_provider)
			dns_provider = dns.for_domain_provider(domain_row.domain_provider)
			issued = tls_provider.issue(domain_row.domain, dns_provider)
		except Exception:
			self._set_status("Failed")
			raise

		self.db_set(
			{
				"fullchain_path": issued.fullchain_path,
				"privkey_path": issued.privkey_path,
				# not_before/not_after arrive as raw OpenSSL date strings
				# (`Jun  8 07:32:49 2026 GMT`); normalize to the Datetime columns'
				# format, the step scripts/lib/atlas/certs.py documents.
				"issued_on": frappe.utils.get_datetime(issued.not_before),
				"expires_on": frappe.utils.get_datetime(issued.not_after),
				"tls_provider": domain_row.tls_provider,
				"status": "Active",
			}
		)
		self._push_to_proxies()

	# --- Push to proxies (the wiring to the proxy VM) --------------------

	@frappe.whitelist()
	def push_to_proxies(self) -> list[str]:
		"""Push to Proxies button — re-push the current PEMs without re-issuing."""
		return self._push_to_proxies()

	def _push_to_proxies(self) -> list[str]:
		"""Read the PEMs off disk and call `proxy.push_cert` for every proxy VM in
		the domain's region. Returns the names of the proxy VMs pushed to. A proxy
		that can't be reached is logged and skipped — one wedged guest never wedges
		the fan-out (mirrors `proxy.reconcile_region`)."""
		if not self.fullchain_path or not self.privkey_path:
			frappe.throw(_("TLS Certificate has no PEM paths; issue it first"))
		fullchain = _read_pem(self.fullchain_path)
		privkey = _read_pem(self.privkey_path)
		region = frappe.db.get_value("Root Domain", self.root_domain, "region")
		if not region:
			frappe.throw(f"Root Domain {self.root_domain} has no region")

		pushed: list[str] = []
		for vm_name in proxy._proxy_vms_in_region(region):
			try:
				proxy.push_cert(vm_name, fullchain, privkey)
				pushed.append(vm_name)
			except Exception as exception:
				frappe.log_error(f"Cert push failed for {vm_name}: {exception}", "TLS Certificate push")

		# Point the regional wildcard at the proxy fleet. The cert proves identity;
		# this record makes `<sub>.<domain>` actually resolve to the proxies (A →
		# their reserved IPv4, AAAA → their /128). Reconciled here so a fleet change
		# (rebuild, reserved-IP reattach) is reflected on the next push, mirroring
		# the cert fan-out. A DNS failure is logged, not raised: the cert is already
		# on the proxies, and a stale wildcard shouldn't undo a successful push.
		self._publish_wildcard(region)
		return pushed

	def _publish_wildcard(self, region: str) -> None:
		domain_row = frappe.get_doc("Root Domain", self.root_domain)
		ipv4, ipv6 = proxy.wildcard_targets_for_region(region)
		if not ipv4 and not ipv6:
			frappe.log_error(
				f"No proxy addresses in region {region!r}; wildcard for {domain_row.domain} not published",
				"TLS Certificate DNS",
			)
			return
		try:
			dns_provider = dns.for_domain_provider(domain_row.domain_provider)
			records = dns_provider.upsert_wildcard(
				domain_row.domain, dns.WildcardTargets(ipv4=ipv4, ipv6=ipv6)
			)
			frappe.logger().info(f"Published wildcard for {domain_row.domain}: {records}")
		except Exception as exception:
			frappe.log_error(
				f"Wildcard DNS upsert failed for {domain_row.domain}: {exception}",
				"TLS Certificate DNS",
			)

	def _set_status(self, status: str) -> None:
		self.db_set("status", status)


def _read_pem(path: str) -> str:
	expanded = pathlib.Path(path).expanduser()
	if not expanded.is_file():
		frappe.throw(f"PEM not found at {path!r}")
	return expanded.read_text()


# --- Scheduled renewal ---------------------------------------------------


def renew_expiring() -> None:
	"""Daily scheduler entry point (atlas/hooks.py). Renew every Active cert whose
	`expires_on` falls within RENEWAL_WINDOW_DAYS — re-issue AND re-push, then the
	status returns to Active. Mirrors the proxy reconcile philosophy: the desired
	state (a fresh cert on every proxy) is continuously restored."""
	cutoff = frappe.utils.add_days(frappe.utils.now_datetime(), RENEWAL_WINDOW_DAYS)
	due = frappe.get_all(
		"TLS Certificate",
		filters={"status": "Active", "expires_on": ["<=", cutoff]},
		pluck="name",
	)
	for name in due:
		try:
			frappe.get_doc("TLS Certificate", name).renew()
		except Exception as exception:
			frappe.log_error(f"Renewal failed for {name}: {exception}", "TLS Certificate renew")
