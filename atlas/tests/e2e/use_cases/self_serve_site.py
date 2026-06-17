"""Use case: signup → email verify → live Frappe site, end to end.

The superset host-bound proof of the self-serve site layer
(spec/14-self-serve.md). It consumes everything
the other tracks built — the golden bench snapshot, the `Site` doctype, the
`deploy-site.py` + readiness gate, the signup/verify on-ramp — PLUS the already
proven proxy + TLS layers, and drives the whole flow on a real droplet:

    signup (request_site)  →  Site Request (Pending), NO Site/VM   (Contract C)
        ↓ verify()
    User + Site (owner = verified user)                            (Contract C)
        ↓ Site.after_insert → auto_provision (worker)
    clone golden snapshot → boot → deploy-site.py → HTTP 200       (01/03, Contract B)
        ↓
    Subdomain row → proxy reconcile → live at acme.<region>.<domain>
        ↓
    off-droplet HTTPS on v4 (reserved IP) AND v6 (proxy /128)      (the idea-doc requirement)

What ONLY a live run can prove (everything below is unit-covered otherwise — the
Site state machine in test_site.py, the validators in test_subdomain_label.py,
the signup ordering in test_api_signup.py):

- **The golden image actually serves.** A VM cloned from 01's snapshot, after
  03's deploy-site.py, answers HTTP 200 on :80 for its Host header — the full
  bake → clone → boot → deploy chain survives onto a fresh per-site VM.
- **The readiness signal is real.** The Site flips to Running only on an observed
  200 (Contract B), driven by the real `auto_provision` worker job, not on the
  VM's status.
- **The proxy routes the new subdomain end to end.** Once auto_provision creates
  the Subdomain, an OFF-droplet request to https://<sub>.<region>.<domain> reaches
  the site through the proxy — over BOTH the reserved IPv4 and public IPv6.
- **Verification gates provision (Contract C).** request_site creates a Pending
  Site Request and NOTHING billable (no Site, no VM) until verify() consumes the
  token.

Cost: the proxy_vm infra (one shared droplet, a proxy VM, one reserved IPv4) PLUS
a real ACME issuance against a real Route 53 zone (LE staging) PLUS a golden-image
site VM cloned + deployed. It needs the TLS config keys (`atlas_tls_*`, see
`_config.get_tls_config`), certbot + boto3 on the controller, and a golden bench
snapshot (resolved from Atlas Settings, or baked inline if absent). Absent any of
those it raises before any billable provision (MissingConfig / a clear preflight
error), mirroring tls_issuance.

    bench --site atlas.tests.local execute \
        atlas.tests.e2e.use_cases.self_serve_site.run_smoke

It is NOT folded into `run_all_smoke`: it needs a live AWS zone, an ACME round
trip, and a special golden snapshot, so it owns its run like tls_issuance /
server_provisioning do.
"""

import subprocess
import time

import frappe

from atlas.atlas import proxy
from atlas.tests.e2e._config import MissingConfig, get_tls_config
from atlas.tests.e2e._shared import phase
from atlas.tests.e2e.use_cases import bench_image, tls_issuance
from atlas.tests.e2e.use_cases.proxy_vm import (
	_allocate_and_attach,
	_assert_live_map,
	_provision_proxy_vm,
)
from atlas.tests.e2e.use_cases.proxy_vm import _teardown as _teardown_proxy

# The subdomain label the run claims (Contract A: a single dotless DNS label,
# inside the regional wildcard). Stable across runs so a leaked row is obvious.
_TEST_SUBDOMAIN = "acme"
# A throwaway email — the verified User that fulfilment creates is dropped in
# teardown (a real User row persists past the transaction; the e2e is
# non-transactional).
_TEST_EMAIL = "self-serve-e2e@example.com"


def run(reuse: bool = True, keep: bool = True) -> None:
	"""Full path: the smoke flow PLUS the Contract-C negative assertion under one
	umbrella. The validator throws (Contract-A label/denylist) are unit-covered
	(test_subdomain_label, test_site, test_api_signup); the only thing `run` adds
	over `run_smoke` is asserting, on the real path, that an UNVERIFIED request
	provisions nothing — which `run_smoke` also does inline before fulfilment."""
	run_smoke(reuse=reuse, keep=keep)


def run_smoke(reuse: bool = True, keep: bool = True) -> None:
	config = get_tls_config()
	tls_issuance._preflight_controller_deps()

	with phase("self-serve-site (smoke)", reuse=reuse, keep=keep) as server:
		region = config["region"]
		domain = config["domain"]

		# Single-active-Root-Domain invariant: active_root_domain() throws on more
		# than one active row, and the live site may already carry one. Quiet any
		# others for the run, seed ours, restore in teardown.
		quieted = _quiet_other_root_domains(domain)
		tls_issuance._seed_tls_doctypes(config)

		# The golden bench snapshot the site VM clones from (spec/08-images.md). Resolve it
		# from Atlas Settings, or bake it inline on the shared droplet if absent —
		# fail clean (MissingConfig) before any billable site provision if neither.
		# Side effect (sets Atlas Settings.default_bench_snapshot) is what matters here.
		_resolve_or_bake_golden_snapshot(server.name)

		proxy_vm = _provision_proxy_vm(server.name, _base_image(server.name), region)
		reserved = None
		try:
			# 1. Build the proxy + issue and push the REAL wildcard cert. The proxy
			#    must be serving :443 with the cert before the site comes up so the
			#    off-droplet HTTPS probes have a front door.
			proxy.build_proxy(proxy_vm.name)
			cert_name = tls_issuance._issue_certificate(domain)
			frappe.get_doc("TLS Certificate", cert_name).push_to_proxies()
			reserved = _allocate_and_attach(server.name, proxy_vm.name)
			reserved_ipv4 = frappe.db.get_value("Reserved IP", reserved, "ip_address")

			# 2. Contract C — the negative: signup creates a Pending Site Request and
			#    NOTHING billable. Assert no Site, no VM, before we verify.
			request_name = _request_site(_TEST_EMAIL, _TEST_SUBDOMAIN)
			_assert_no_provision_yet(_TEST_SUBDOMAIN, domain)

			# 3. Verify (skip SMTP by calling verify() directly, the same method the
			#    /verify route calls). It creates the User + inserts the Site as owner;
			#    the Site's after_insert enqueues auto_provision, which the worker runs
			#    on the real droplet: clone golden snapshot → boot → deploy → 200 →
			#    Subdomain → Running. This is the whole chain under test.
			fqdn = _verify_request(request_name)
			site_vm_name = _wait_for_site_running(fqdn)
			_assert_admin_password_set(fqdn)

			# 4. The proxy routes the new subdomain. auto_provision already created the
			#    Subdomain; reconcile the proxy to it and read the live map back.
			proxy.reconcile_proxy(proxy_vm.name)
			site_vm = frappe.get_doc("Virtual Machine", site_vm_name)
			_assert_live_map(proxy_vm.name, {_TEST_SUBDOMAIN: site_vm.ipv6_address})

			# 5. Off-droplet HTTPS on BOTH v4 (reserved IP) and v6 (proxy /128) — the
			#    idea-doc "works on IPv4 and IPv6" requirement, proven from the
			#    controller's honest off-droplet vantage. LE staging is untrusted, so
			#    curl -k; the served cert is byte-identical to the issued one is proven
			#    separately by tls_issuance.
			hostname = f"{_TEST_SUBDOMAIN}.{domain}"
			_assert_inbound_https("-4", reserved_ipv4, hostname)
			_assert_inbound_https("-6", proxy_vm.ipv6_address, hostname)
			print(f"[e2e] self-serve site live at https://{hostname} over v4 AND v6 OK")
		finally:
			_teardown(reserved, proxy_vm.name, _TEST_SUBDOMAIN, domain, _TEST_EMAIL)
			tls_issuance._cleanup_tls_doctypes(config)
			_restore_root_domains(quieted)


# --- golden snapshot + base image ----------------------------------------


def _base_image(server_name: str) -> str:
	"""The plain Ubuntu base image on the shared server (the proxy VM boots from it;
	the SITE VM clones from the golden snapshot instead)."""
	from atlas.tests.e2e._image import ensure_image_on_server

	return ensure_image_on_server(server_name).name


def _resolve_or_bake_golden_snapshot(server_name: str) -> str:
	"""Return an Available golden bench snapshot for the site VM to clone from, and
	make sure `Atlas Settings.default_bench_snapshot` points at it (Site.auto_provision
	resolves it via placement.default_bench_snapshot).

	Resolve-or-bake (operator decision): use the configured snapshot if it exists
	and is Available; otherwise bake one inline on the shared droplet via
	bench_image (slow: apt + clone + uv + node) and configure it. The bake is the
	plan-01 host fact; here it is a precondition, not the thing under test."""
	configured = frappe.db.get_single_value("Atlas Settings", "default_bench_snapshot")
	if configured and frappe.db.get_value("Virtual Machine Snapshot", configured, "status") == "Available":
		print(f"[e2e] golden bench snapshot (configured): {configured}")
		return configured

	print("[e2e] no Available golden bench snapshot configured — baking one inline (slow) ...")
	summary = bench_image._bake(server_name)
	snapshot = summary["snapshot"]
	frappe.db.set_single_value("Atlas Settings", "default_bench_snapshot", snapshot, update_modified=False)
	frappe.db.commit()
	print(f"[e2e] baked + configured golden bench snapshot: {snapshot}")
	# Leave the build VM for teardown to terminate by name (the snapshot is the
	# artifact, but the build VM is e2e scratch — don't strand it).
	_BAKED_BUILD_VMS.append(summary["build_vm"])
	return snapshot


# Build VMs baked in this run (terminated in teardown; the snapshot survives).
_BAKED_BUILD_VMS: list[str] = []


# --- Root Domain invariant -----------------------------------------------


def _quiet_other_root_domains(keep_domain: str) -> list[str]:
	"""Deactivate every active Root Domain except the one this run seeds, so
	active_root_domain() resolves unambiguously. Returns the names quieted (to
	reactivate in teardown). The seeded row (keep_domain) is left alone — seeding
	recreates it active."""
	quieted = []
	for name in frappe.get_all("Root Domain", filters={"is_active": 1}, pluck="name"):
		if name == keep_domain:
			continue
		frappe.db.set_value("Root Domain", name, "is_active", 0)
		quieted.append(name)
	if quieted:
		frappe.db.commit()
		print(f"[e2e] quieted {len(quieted)} other active Root Domain(s) for the run: {quieted}")
	return quieted


def _restore_root_domains(quieted: list[str]) -> None:
	for name in quieted:
		if frappe.db.exists("Root Domain", name):
			frappe.db.set_value("Root Domain", name, "is_active", 1)
	if quieted:
		frappe.db.commit()


# --- signup / verify (Contract C) ----------------------------------------


def _request_site(email: str, subdomain: str) -> str:
	"""Drive the real signup endpoint and return the Site Request name.

	Calls `request_site.__wrapped__` to bypass the rate-limit decorator (it needs a
	request context; the underlying logic is what we exercise — same shape the unit
	tests use). Inserts a Pending Site Request and queues the verification mail; it
	does NOT create a Site/VM (asserted next)."""
	from atlas.atlas.api.signup import request_site

	result = request_site.__wrapped__(email=email, subdomain=subdomain)
	frappe.db.commit()
	name = frappe.db.get_value("Site Request", {"email": email, "subdomain": subdomain, "status": "Pending"})
	assert name, f"request_site did not leave a Pending Site Request: {result}"
	print(f"[e2e] signup -> Site Request {name} (Pending), no provision yet")
	return name


def _assert_no_provision_yet(subdomain: str, domain: str) -> None:
	"""Contract C, the negative: an unverified request provisions nothing. No Site
	row for the FQDN, and no Virtual Machine titled for it. Billable work only after
	the token is consumed."""
	fqdn = f"{subdomain}.{domain}"
	assert not frappe.db.exists("Site", fqdn), f"Site {fqdn} exists before verification (Contract C broken)"
	assert not frappe.db.exists("Virtual Machine", {"title": fqdn}), (
		f"a VM for {fqdn} exists before verification (Contract C broken)"
	)
	print(f"[e2e] Contract C: no Site / no VM for {fqdn} before verification OK")


def _verify_request(request_name: str) -> str:
	"""Fulfil the request the way the /verify route does: SiteRequest.verify()
	creates the User, inserts the Site as owner, and (via the Site's after_insert)
	enqueues auto_provision on the worker. Returns the created Site's FQDN."""
	request = frappe.get_doc("Site Request", request_name)
	site = request.verify()
	frappe.db.commit()
	assert site and frappe.db.exists("Site", site.name), f"verify() did not create a Site: {site}"
	print(f"[e2e] verify -> User {request.email} owns Site {site.name}; auto_provision enqueued")
	return site.name


# --- readiness (Contract B, worker-driven) -------------------------------


def _wait_for_site_running(fqdn: str, timeout_seconds: int = 1800) -> str:
	"""Block until the auto_provision worker job flips the Site to Running — the
	full clone → boot → deploy → HTTP 200 → Subdomain chain (Contract B), driven by
	the REAL worker, not inline. Returns the backing VM name once known.

	Polls with rollback() so we read the worker's committed writes (db_set commits
	per step). Raises on Failed (the job marked it) or on the deadline (the worker
	didn't pick it up, or a step hung). Long timeout: the chain clones a VM, boots
	it, resets the admin password on the baked site, and waits for the 200 (a warm
	clone is already serving; a cold clone also runs setup production)."""
	deadline = time.monotonic() + timeout_seconds
	last_status = None
	while time.monotonic() < deadline:
		frappe.db.rollback()
		status = frappe.db.get_value("Site", fqdn, "status")
		if status != last_status:
			elapsed = int(time.monotonic() - (deadline - timeout_seconds))
			print(f"[e2e] Site {fqdn} status={status!r} (t+{elapsed}s)")
			last_status = status
		if status == "Running":
			vm_name = frappe.db.get_value("Site", fqdn, "virtual_machine")
			assert vm_name, f"Site {fqdn} is Running but has no backing VM"
			print(f"[e2e] Site {fqdn} Running on VM {vm_name} OK")
			return vm_name
		if status == "Failed":
			_dump_site_tasks(fqdn)
			raise AssertionError(f"Site {fqdn} reached Failed during auto_provision")
		time.sleep(5)
	_dump_site_tasks(fqdn)
	raise AssertionError(
		f"Site {fqdn} did not reach Running within {timeout_seconds}s "
		f"(auto_provision worker didn't run, or a step hung)"
	)


def _dump_site_tasks(fqdn: str) -> None:
	"""On timeout/Failed, print the recent guest Tasks (deploy-site, etc.) for the
	backing VM so the operator sees where the chain stalled."""
	vm_name = frappe.db.get_value("Site", fqdn, "virtual_machine")
	if not vm_name:
		print(f"[e2e] Site {fqdn} has no backing VM yet (clone/provision never completed)")
		return
	for task in frappe.get_all(
		"Task",
		filters={"virtual_machine": vm_name},
		fields=["name", "script", "status", "creation"],
		order_by="creation desc",
		limit=5,
	):
		print(f"[e2e]   task {task.name} script={task.script} status={task.status} ({task.creation})")


def _assert_admin_password_set(fqdn: str) -> None:
	"""The Administrator password handed to the owner — the SHARED baked throwaway
	(the rename model dropped the per-VM reset; the owner rotates it after first
	login) — is stored encrypted on the Site and readable by the owner. Assert it is
	non-empty — the backend reveal the SPA will surface (the SPA Sites screen is
	deferred). We don't log in here: LE staging is untrusted (curl -k) and a real
	Desk login adds nothing this proves over the 200 + password presence."""
	password = frappe.get_doc("Site", fqdn).get_password("admin_password")
	assert password, f"Site {fqdn} has no admin_password stored after Running"
	print(f"[e2e] admin password stored on {fqdn} ({len(password)} chars) OK")


# --- off-droplet HTTPS (v4 + v6) -----------------------------------------


def _assert_inbound_https(family: str, address: str, hostname: str) -> None:
	"""From the controller (off the droplet), HTTPS to `address` (a v4 reserved IP
	or the proxy's v6 /128) with SNI/Host forced to `hostname`, and assert a 200
	comes back through the proxy from the live Frappe site.

	`family` is curl's `-4`/`-6`. The probe is `/api/method/ping` (the same honest
	"Frappe is serving THIS site" signal the readiness gate uses) so a real
	site response (`pong`) is the success token — independent of the setup-wizard.
	curl -k (LE staging is untrusted; cert identity is tls_issuance's job). Polls
	for the DO edge / DNAT / nginx / fresh DNS to settle. v6 needs brackets in the
	URL but the bare literal in --resolve (the `v6 needs brackets` trap)."""
	url_host = f"[{address}]" if family == "-6" else address
	deadline = time.monotonic() + 240
	last = ""
	while time.monotonic() < deadline:
		try:
			result = subprocess.run(
				[
					"curl",
					family,
					"-k",
					"-sS",
					"--max-time",
					"15",
					"--resolve",
					f"{hostname}:443:{address}",
					f"https://{hostname}/api/method/ping",
				],
				capture_output=True,
				text=True,
				timeout=30,
			)
			if result.returncode == 0 and "pong" in result.stdout:
				print(f"[e2e] inbound :443 {family} {url_host} ({hostname}) -> proxy -> site (pong) OK")
				return
			last = (result.stderr or result.stdout).strip()
		except subprocess.TimeoutExpired:
			last = "curl timed out"
		time.sleep(5)
	raise AssertionError(
		f"inbound HTTPS {family} to {url_host} ({hostname}) never routed to the site within 240s "
		f"(last: {last!r}). The reserved-IP DNAT (v4) / proxy /128 (v6), the pushed cert, the live "
		f"map, or the proxy→site v6 hop is broken — or the controller has no {family} path to it."
	)


# --- teardown ------------------------------------------------------------


def _teardown(
	reserved: str | None,
	proxy_vm_name: str,
	subdomain: str,
	domain: str,
	email: str,
) -> None:
	"""Billable-aware teardown, every step guarded so one failure doesn't strand the
	rest (terminate the site VM, delete the Subdomain, release the reserved
	IP, delete the Site / Site Request rows, and the created User — which persists
	past the transaction). Site.terminate() already drops the Subdomain + the backing
	VM, so we drive it first, then mop up the rows it doesn't own."""
	fqdn = f"{subdomain}.{domain}"

	# 1. Site.terminate(): drops the Subdomain (proxy stops routing on reconcile)
	#    and terminates the backing VM. Then delete the Site row itself.
	if frappe.db.exists("Site", fqdn):
		try:
			site = frappe.get_doc("Site", fqdn)
			if site.status != "Terminated":
				site.terminate()
				frappe.db.commit()
			frappe.delete_doc("Site", fqdn, force=1, ignore_permissions=True)
			frappe.db.commit()
		except Exception:
			_warn(f"Site {fqdn} teardown failed — terminate/delete it by hand")

	# 2. The Site Request + the created User (non-transactional rows that outlive
	#    the run). Delete the request first (it links the Site), then the User.
	for request in frappe.get_all("Site Request", filters={"email": email}, pluck="name"):
		try:
			frappe.delete_doc("Site Request", request, force=1, ignore_permissions=True)
		except Exception:
			_warn(f"Site Request {request} delete failed")
	if frappe.db.exists("User", email):
		try:
			frappe.delete_doc("User", email, force=1, ignore_permissions=True)
		except Exception:
			_warn(f"User {email} delete failed")
	frappe.db.commit()

	# 3. The reserved IP + the proxy VM (reuse proxy_vm's teardown). The site VM is
	#    already gone via Site.terminate, so pass the proxy name for both slots —
	#    the second pass is a guarded no-op (exists + status != Terminated).
	_teardown_proxy(reserved, proxy_vm_name, proxy_vm_name)

	# 4. Any build VMs baked inline this run (the snapshot survives as the artifact).
	for vm_name in _BAKED_BUILD_VMS:
		if frappe.db.exists("Virtual Machine", vm_name):
			vm = frappe.get_doc("Virtual Machine", vm_name)
			if vm.status != "Terminated":
				try:
					vm.terminate()
					frappe.db.commit()
				except Exception:
					_warn(f"build VM {vm_name} terminate failed")
	_BAKED_BUILD_VMS.clear()


def _warn(message: str) -> None:
	import traceback

	print(f"[e2e] WARNING: {message}:")
	traceback.print_exc()
