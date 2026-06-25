"""Use case: issue a real regional wildcard cert and prove it lands on the proxy.

This is the host/live-API proof of the TLS & domain layer (spec/13-tls.md). The
unit suite covers the controller state machine, the registries, and the certbot
argv with everything mocked; only this module exercises the *real* producer chain
that nothing else does:

    Root Domain.issue_certificate()
      → TLS Certificate.issue()
        → LetsEncryptProvider.issue()          (real certbot subprocess)
          → certbot DNS-01 via Route53DnsProvider  (real AWS Route 53 zone)
            → PEMs on the controller's disk
        → _push_to_proxies() → proxy.push_cert(...)  (real proxy guest, nginx reload)

What it proves, end to end, that only a live run can:

- **Route 53 reachability** — `Route53DnsProvider.authenticate()` lists hosted zones
  with the configured IAM creds (the same `route53:*` issuance needs).
- **Real ACME DNS-01 issuance** — certbot runs on the controller, plants the TXT
  record in the real zone, and Let's Encrypt (staging) issues `*.<domain>`. Proves
  `local_task.run_local_task` + `issue-cert.py` + `scripts/lib/atlas/certs.py`
  against real certbot — the controller-local Task transport's only real exercise.
- **PEMs on disk + parsed validity** — `fullchain_path`/`privkey_path` exist, the
  cert is the wildcard, and `issued_on`/`expires_on` are populated from the cert.
- **A Task row is recorded** — issuance shows up in the same audit list as every
  host/guest op (the `issue-cert.py` controller Task).
- **Producer → push_cert → nginx, end to end** — the LE-issued PEMs are pushed to
  a real proxy guest and an off-droplet `:443` request routes through the proxy to
  the site, served under the freshly issued cert. (LE *staging* is untrusted, so
  the probe is `curl -k`, like proxy_vm's self-signed path — what is being proven
  here is that the REAL producer feeds push_cert, not chain trust.)

Cost: the proxy_vm infra (one droplet, two VMs, one reserved IPv4) PLUS a real
ACME issuance against a real Route 53 zone. It needs the TLS config keys
(`atlas_tls_domain` etc., see `_config.get_tls_config`) and certbot +
certbot-dns-route53 + boto3 installed on the controller; absent either, it raises
`MissingConfig` / a clear preflight error and runs nothing billable.

    bench --site atlas.tests.local execute atlas.tests.e2e.use_cases.tls_issuance.run_smoke

It is NOT folded into `run_all_smoke`: it needs a live AWS zone + an ACME round
trip, so it owns its run like `server_provisioning`/`digitalocean_client` do.
"""

import shutil
import subprocess
import time

import frappe

from atlas.atlas import proxy
from atlas.tests.e2e._config import MissingConfig, get_tls_config
from atlas.tests.e2e._shared import (
	ensure_image_on_server,
	phase,
)

# Reuse the proxy_vm helpers verbatim — the proxy side is already proven there;
# this module swaps the self-signed cert for a real LE-issued one and adds the
# producer chain in front of the push.
#
# NOTE: proxy_vm's own `:443` probe hardcodes the hostname as
# `<sub>.<region>.frappe.dev`, baking in the convention that the wildcard zone IS
# `<region>.frappe.dev`. This module issues for an arbitrary `config["domain"]`
# (e.g. `atlas1.x.frappe.dev`), so it CANNOT reuse that probe — it builds the
# hostname from the cert's real domain (`_assert_inbound_https_routes_to_domain`
# below). The map key is still the bare subdomain label (`map_for_region` keys on
# `subdomain`), so the served cert `*.<domain>` matches SNI `<sub>.<domain>`.
from atlas.tests.e2e.use_cases.proxy_vm import (
	_TEST_SUBDOMAIN,
	_allocate_and_attach,
	_assert_live_map,
	_make_subdomain,
	_provision_proxy_vm,
	_provision_site_vm,
	_start_site_server,
	_teardown,
)

# Active vendor types the harness configures on the Settings singles each run.
_DNS_PROVIDER_TYPE = "Route53"
_TLS_PROVIDER_TYPE = "Let's Encrypt"


def run(reuse: bool = True, keep: bool = True) -> None:
	"""Full path. Same as run_smoke — the whole use case is host/live-API bound, so
	there is no extra unit-coverable layer here (the controller logic, registries,
	and certbot argv are unit-tested with the external edges mocked)."""
	run_smoke(reuse=reuse, keep=keep)


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	config = get_tls_config()
	_preflight_controller_deps()

	with phase("tls-issuance (smoke)", reuse=reuse, keep=keep) as server:
		region = config["region"]
		domain = config["domain"]
		image = ensure_image_on_server(server.name).name

		_seed_tls_doctypes(config)
		try:
			# 1. Route 53 reachability — the same read issuance depends on.
			_assert_route53_reachable()

			# 2. Real ACME issuance: certbot DNS-01 against the live zone, LE staging.
			#    This drives the controller Task + issue-cert.py + real certbot.
			cert_name = _issue_certificate(domain)
			_assert_real_cert_on_disk(cert_name, domain)
			_assert_issue_task_recorded(domain)

			# 3. Stand up a proxy + site and prove the REAL cert routes :443.
			proxy_vm = _provision_proxy_vm(server.name, image, region)
			site_vm = _provision_site_vm(server.name, image)
			reserved = None
			try:
				proxy.build_proxy(proxy_vm.name)
				marker = f"marker-{site_vm.name[:8]}"
				_start_site_server(server.name, site_vm, marker)
				_make_subdomain(_TEST_SUBDOMAIN, site_vm.name, region)

				synced = proxy.reconcile_proxy(proxy_vm.name)
				assert synced, "first reconcile should have drifted (fresh proxy, empty dict)"
				_assert_live_map(proxy_vm.name, {_TEST_SUBDOMAIN: site_vm.ipv6_address})

				reserved = _allocate_and_attach(server.name, proxy_vm.name)
				reserved_ipv4 = frappe.db.get_value("Reserved IP", reserved, "ip_address")

				# THE WIRING UNDER TEST: push the LE-issued PEMs (not a self-signed
				# stand-in) to the proxy fleet via the producer's own fan-out.
				pushed = frappe.get_doc("TLS Certificate", cert_name).push_to_proxies()
				assert proxy_vm.name in pushed, (
					f"_push_to_proxies did not reach the proxy VM; pushed={pushed}"
				)

				# LE staging is untrusted, so the off-droplet probe is curl -k — what
				# this asserts is that the REAL producer fed push_cert and the proxy
				# serves :443 routing to the site under the issued wildcard. The
				# hostname is <sub>.<cert-domain>, so SNI matches *.<domain>.
				_assert_inbound_https_routes_to_domain(reserved_ipv4, domain, marker)
				_assert_served_cert_is_issued(reserved_ipv4, domain, cert_name)
				print("[e2e] LE-issued wildcard pushed via producer -> proxy serves :443 OK")
			finally:
				_teardown(reserved, proxy_vm.name, site_vm.name)
		finally:
			_cleanup_tls_doctypes(config)


# --- preflight -----------------------------------------------------------


def _preflight_controller_deps() -> None:
	"""Fail fast, before any billable provision, if the controller-host deps the
	TLS layer needs are missing (spec/13-tls.md: certbot + certbot-dns-route53 +
	openssl, plus boto3 for the Route 53 authenticate read)."""
	missing = [binary for binary in ("certbot", "openssl") if shutil.which(binary) is None]
	if missing:
		raise RuntimeError(
			f"controller is missing TLS dependencies: {', '.join(missing)}. "
			"Install certbot + certbot-dns-route53 (and openssl) on the Atlas controller "
			"(spec/13-tls.md, 'controller-host dependency')."
		)
	try:
		import boto3
	except ImportError as exception:
		raise RuntimeError(
			"controller is missing boto3 (needed for Route 53 authenticate / certbot-dns-route53). "
			"pip install boto3 certbot-dns-route53 on the Atlas controller."
		) from exception


# --- DocType seeding -----------------------------------------------------


def _seed_tls_doctypes(config: dict) -> None:
	"""Configure the TLS-layer Settings singles + the Root Domain from config,
	idempotently. Mirrors the operator first-run order (spec/13-tls.md): set the
	active DNS vendor on Route53 Settings, the active TLS issuer on Atlas Settings,
	their credential singles, then the Root Domain (which denormalizes both vendor
	types in before_insert)."""
	import frappe.utils.password

	_cleanup_tls_doctypes(config)  # start from a clean slate (immutable fields)

	# Atlas Settings.region is THE single source of truth for this instance's region
	# (atlas_region()): the Site derives its region from it, Root Domain denormalizes
	# it at insert, and the proxy fleet joins on it. Seed it FIRST so all three agree
	# — mirrors the operator first-run order. Without this the Root Domain below is
	# forced to config["region"] while the Site keeps a stale Atlas Settings.region,
	# so the Subdomain lands in a different region than the proxy VM and the proxy's
	# live map comes back empty (map_for_region finds no active row for the proxy's
	# region).
	frappe.db.set_single_value("Atlas Settings", "region", config["region"], update_modified=False)
	frappe.db.set_single_value(
		"Atlas Settings", "dns_provider_type", _DNS_PROVIDER_TYPE, update_modified=False
	)
	frappe.db.set_single_value(
		"Atlas Settings", "tls_provider_type", _TLS_PROVIDER_TYPE, update_modified=False
	)
	frappe.db.set_single_value(
		"Route53 Settings", "access_key_id", config["access_key_id"], update_modified=False
	)
	frappe.db.set_single_value("Route53 Settings", "region", config["aws_region"], update_modified=False)
	frappe.utils.password.set_encrypted_password(
		"Route53 Settings", "Route53 Settings", config["secret_access_key"], "secret_access_key"
	)
	frappe.db.set_single_value(
		"Lets Encrypt Settings", "acme_directory_url", config["acme_directory_url"], update_modified=False
	)
	frappe.db.set_single_value(
		"Lets Encrypt Settings", "account_email", config["account_email"], update_modified=False
	)

	frappe.get_doc(
		{
			"doctype": "Root Domain",
			"domain": config["domain"],
			"region": config["region"],
			"dns_provider_type": _DNS_PROVIDER_TYPE,
			"tls_provider_type": _TLS_PROVIDER_TYPE,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	print(f"[e2e] seeded TLS doctypes for {config['domain']} (region {config['region']})")


def _cleanup_tls_doctypes(config: dict) -> None:
	"""Drop the cert + the Root Domain this run owns (the Settings Singles are left
	configured — they hold no per-run identity and the next run overwrites them).
	Guarded so a half-seeded run still cleans up."""
	domain = config["domain"]
	for cert in frappe.get_all("TLS Certificate", filters={"root_domain": domain}, pluck="name"):
		frappe.delete_doc("TLS Certificate", cert, force=1, ignore_permissions=True)
	if frappe.db.exists("Root Domain", domain):
		frappe.delete_doc("Root Domain", domain, force=1, ignore_permissions=True)
	frappe.db.commit()


# --- assertions ----------------------------------------------------------


def _assert_route53_reachable() -> None:
	"""Route53 DNS provider authenticate() against the real account — the
	GetHostedZone read that proves the IAM creds carry the route53 permissions
	issuance needs."""
	from atlas.atlas import dns

	result = dns.for_dns_provider_type(_DNS_PROVIDER_TYPE).authenticate()
	assert result.ok, f"Route 53 authenticate failed: {result.error}"
	print(f"[e2e] Route 53 reachable (account: {result.account_label}) OK")


def _issue_certificate(domain: str) -> str:
	"""Click Issue / Renew Certificate on the Root Domain — the real producer pass.
	Returns the TLS Certificate name."""
	print(f"[e2e] issuing *.{domain} via certbot DNS-01 against the live zone (LE staging) ...")
	cert_name = frappe.get_doc("Root Domain", domain).issue_certificate()
	frappe.db.commit()
	status = frappe.db.get_value("TLS Certificate", cert_name, "status")
	assert status == "Active", f"cert {cert_name} ended {status}, expected Active"
	return cert_name


def _assert_real_cert_on_disk(cert_name: str, domain: str) -> None:
	"""The issued cert's PEM paths exist on the controller, certify the wildcard,
	and the validity window made it onto the row."""
	import pathlib

	cert = frappe.get_doc("TLS Certificate", cert_name)
	assert cert.common_name == f"*.{domain}", cert.common_name
	for path in (cert.fullchain_path, cert.privkey_path):
		assert path and pathlib.Path(path).expanduser().is_file(), f"PEM missing on disk: {path!r}"
	assert cert.issued_on and cert.expires_on, (
		f"validity window not recorded: issued_on={cert.issued_on} expires_on={cert.expires_on}"
	)
	fullchain = pathlib.Path(cert.fullchain_path).expanduser().read_text()
	assert "BEGIN CERTIFICATE" in fullchain, "fullchain.pem is not a PEM certificate"
	print(
		f"[e2e] real cert on disk for *.{domain}: {cert.fullchain_path} "
		f"(valid {cert.issued_on} … {cert.expires_on}) OK"
	)


def _assert_issue_task_recorded(domain: str) -> None:
	"""Issuance recorded a controller-local Task (issue-cert.py), so a cert shows
	up in the same audit list as every host/guest op."""
	rows = frappe.get_all(
		"Task",
		filters={"script": "issue-cert.py", "status": "Success"},
		fields=["name", "creation"],
		order_by="creation desc",
		limit=1,
	)
	assert rows, "no successful issue-cert.py Task row was recorded for the issuance"
	print(f"[e2e] issue-cert.py Task recorded ({rows[0].name}) OK")


def _probe_hostname(domain: str) -> str:
	"""The hostname the off-droplet probe targets: <sub>.<cert-domain>, so SNI
	matches the issued wildcard `*.<domain>`. (proxy_vm's probe assumes
	`<sub>.<region>.frappe.dev`; this module's domain is arbitrary.)"""
	return f"{_TEST_SUBDOMAIN}.{domain}"


def _assert_inbound_https_routes_to_domain(reserved_ipv4: str, domain: str, marker: str) -> None:
	"""Off-droplet HTTPS to the reserved v4, SNI/Host forced to <sub>.<domain>, and
	assert the site marker comes back through the proxy. Same path proxy_vm proves,
	but the hostname is built from the cert's real domain so SNI matches the served
	wildcard. curl -k (LE staging is untrusted); trust is checked separately by
	`_assert_served_cert_is_issued`. Polls for the DO edge + DNAT + nginx to settle."""
	hostname = _probe_hostname(domain)
	deadline = time.monotonic() + 180
	last = ""
	while time.monotonic() < deadline:
		try:
			result = subprocess.run(
				[
					"curl",
					"-4",
					"-k",
					"-sS",
					"--max-time",
					"15",
					"--resolve",
					f"{hostname}:443:{reserved_ipv4}",
					f"https://{hostname}/",
				],
				capture_output=True,
				text=True,
				timeout=30,
			)
			if result.returncode == 0 and marker in result.stdout:
				print(f"[e2e] inbound :443 {reserved_ipv4} ({hostname}) -> proxy -> site ({marker}) OK")
				return
			last = (result.stderr or result.stdout).strip()
		except subprocess.TimeoutExpired:
			last = "curl timed out"
		time.sleep(5)
	raise AssertionError(
		f"inbound HTTPS to {reserved_ipv4} ({hostname}) never routed to the site within 180s "
		f"(last: {last!r}). The reserved-IP DNAT, the pushed cert, the live map, or the "
		f"proxy→site v6 hop is broken — or the controller has no v4 path to a DO reserved IP."
	)


def _assert_served_cert_is_issued(reserved_ipv4: str, domain: str, cert_name: str) -> None:
	"""The strong proof: the cert the PROXY serves on :443 is byte-for-byte the
	LE-issued one we pushed — not build.sh's placeholder. Pull the leaf the proxy
	presents (openssl s_client over the reserved v4 with SNI <sub>.<domain>) and
	compare its SHA-256 fingerprint to the on-disk fullchain leaf the row points at."""
	import pathlib

	hostname = _probe_hostname(domain)
	served = _served_cert_fingerprint(reserved_ipv4, hostname)
	on_disk = pathlib.Path(frappe.db.get_value("TLS Certificate", cert_name, "fullchain_path")).expanduser()
	expected = _pem_leaf_fingerprint(on_disk.read_text())
	assert served == expected, (
		f"proxy is serving a DIFFERENT cert than the issued one.\n"
		f"served  : {served}\nexpected: {expected}\n"
		f"(the push may have failed or nginx didn't reload to the new cert)"
	)
	print(f"[e2e] proxy serves the LE-issued leaf (fingerprint {served[:24]}…) OK")


def _served_cert_fingerprint(reserved_ipv4: str, hostname: str) -> str:
	"""SHA-256 fingerprint of the leaf cert the proxy presents on :443 for `hostname`,
	fetched off-droplet via openssl s_client (-connect the reserved v4, -servername
	the SNI)."""
	connect = subprocess.run(
		[
			"openssl",
			"s_client",
			"-connect",
			f"{reserved_ipv4}:443",
			"-servername",
			hostname,
		],
		input="",
		capture_output=True,
		text=True,
		timeout=30,
	)
	return _pem_leaf_fingerprint(connect.stdout)


def _pem_leaf_fingerprint(pem_text: str) -> str:
	"""SHA-256 fingerprint of the FIRST certificate in a PEM blob (the leaf)."""
	begin = "-----BEGIN CERTIFICATE-----"
	end = "-----END CERTIFICATE-----"
	start = pem_text.find(begin)
	stop = pem_text.find(end, start)
	assert start != -1 and stop != -1, f"no PEM certificate found in:\n{pem_text[:300]}"
	leaf = pem_text[start : stop + len(end)]
	result = subprocess.run(
		["openssl", "x509", "-noout", "-fingerprint", "-sha256"],
		input=leaf,
		capture_output=True,
		text=True,
		timeout=15,
	)
	assert result.returncode == 0, f"openssl fingerprint failed: {result.stderr}"
	return result.stdout.strip().split("=", 1)[-1]
