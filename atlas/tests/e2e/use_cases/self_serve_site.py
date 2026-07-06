"""Use case: Central create_site → live Frappe site, end to end.

The superset host-bound proof of the self-serve site layer
(spec/14-self-serve.md). It consumes everything
the other tracks built — the golden bench snapshot, the `Site` doctype, the
`deploy-site.py` + readiness gate, the Central-facing `create_site` on-ramp — PLUS
the already proven proxy + TLS layers, and drives the whole flow on a real droplet:

    create_site(team, subdomain)  →  Tenant + Site (Pending)
        ↓ Site.after_insert → auto_provision (worker)
    clone golden snapshot → boot → deploy-site.py → HTTP 200       (01/03, Contract B)
        ↓
    Subdomain row → proxy reconcile → live at acme.<region>.<domain>
        ↓
    off-droplet HTTPS on v4 (reserved IP) AND v6 (proxy /128)      (the idea-doc requirement)

What ONLY a live run can prove (everything below is unit-covered otherwise — the
Site state machine in test_site.py, the validators in test_subdomain_label.py,
the create_site API in test_api_site.py):

- **The golden image actually serves.** A VM cloned from 01's snapshot, after
  03's deploy-site.py, answers HTTP 200 on :80 for its Host header — the full
  bake → clone → boot → deploy chain survives onto a fresh per-site VM.
- **The readiness signal is real.** The Site flips to Running only on an observed
  200 (Contract B), driven by the real `auto_provision` worker job, not on the
  VM's status.
- **The proxy routes the new subdomain end to end.** Once auto_provision creates
  the Subdomain, an OFF-droplet request to https://<sub>.<region>.<domain> reaches
  the site through the proxy — over BOTH the reserved IPv4 and public IPv6.
- **Central drives provision.** `create_site` get-or-creates the Tenant and inserts
  the Site (Pending), whose after_insert enqueues the worker chain — no User, no
  email, no verification (Central already owns the tenant).

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

import json
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
# The Central team this site is provisioned for. create_site get-or-creates a
# Tenant keyed on it. Dropped in teardown (the Tenant row persists past the
# transaction; the e2e is non-transactional).
_TEST_TEAM = "self-serve-e2e-team"


def run(reuse: bool = True, keep: bool = True) -> None:
	"""Alias of run_smoke. The validator throws (Contract-A label/denylist) and the
	create_site/Tenant contract are unit-covered (test_subdomain_label, test_site,
	test_api_site); the live run's value is the clone→deploy→route→HTTPS chain that
	run_smoke drives."""
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

			# 2. Central drives provision: create_site get-or-creates the Tenant and
			#    inserts the Site (Pending). Its after_insert enqueues auto_provision,
			#    which the worker runs on the real droplet: clone golden snapshot →
			#    boot → deploy → 200 → Subdomain → Running. This is the whole chain
			#    under test.
			fqdn = _create_site(_TEST_TEAM, _TEST_SUBDOMAIN)
			site_vm_name = _wait_for_site_running(fqdn)
			_assert_login_url_set(fqdn)

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

			# 6. Bench self-routing (spec/18, one-way push): the SAME running site VM is a
			#    bench VM — its golden clone carries the full bench-cli stack AND the
			#    in-guest `bench-domain-provider` binary (the Phase-1 pilot plug-in). From
			#    INSIDE the guest, `bench-domain-provider register <fqdn>` reserves a name
			#    BEFORE `bench new-site` (the controller resolves the VM from the v6 source
			#    /128, no parameter), the proxy serves it, a forced-create-failure rollback
			#    leaves no stray, drop+deregister stops it, the host-level queries answer,
			#    and a direct terminate cleans up. This is the host fact only a real guest
			#    over IPv6 can prove (the converge logic is unit-covered, test_bench_routing).
			#    Pass the proxy so the step proves the proxy actually SERVES the
			#    guest-reserved site and STOPS on deregister.
			_assert_bench_self_routing(server.name, site_vm_name, proxy_vm, region, domain)
		finally:
			_teardown(reserved, proxy_vm.name, _TEST_SUBDOMAIN, domain, _TEST_TEAM)
			tls_issuance._cleanup_tls_doctypes(config)
			_restore_root_domains(quieted)


# --- bench self-routing (spec/18, the one-way push model) ----------------

# A second subdomain the bench owner spins up from inside the guest — distinct from
# the Atlas-deployed `acme`, so the row `register` reserves is unambiguously the
# guest-created one.
_BENCH_SELF_ROUTE_SUBDOMAIN = "ws"

# A spare label kept only for defensive cleanup of rows older runs may have stranded
# (the old `list`/stray-clear check retired with the move off `atlas-route`).
_BENCH_STRAY_SUBDOMAIN = "stray"

# The fixed, shared MariaDB root password the bake sets (bench/bench.toml
# `root_password`). `drop-site` needs root to drop the site DB and prompts for it over
# a non-interactive SSH session; we pipe this in. Kept in step with bench.toml.
_BAKED_MARIADB_ROOT_PASSWORD = "mariadb-root"


def _install_repo_provider(guest) -> None:
	"""Install the repo's CURRENT bench-domain-provider binary onto the guest at
	/usr/local/bin/bench-domain-provider, so the run exercises the Phase-1 contract even if
	the golden was baked from an older bench/ tree. Ships via base64 to dodge SSH quoting
	hazards (the install /dev/stdin uutils flake gets the /tmp fallback)."""
	import base64
	import os

	import atlas

	app_dir = os.path.dirname(os.path.dirname(os.path.abspath(atlas.__file__)))
	with open(os.path.join(app_dir, "bench", "bench-domain-provider.py")) as handle:
		source = handle.read()
	b64 = base64.b64encode(source.encode()).decode()
	cmd = (
		f"printf '%s' '{b64}' | base64 -d | install -m 0755 /dev/stdin /usr/local/bin/bench-domain-provider "
		f"|| (printf '%s' '{b64}' | base64 -d > /tmp/bench-domain-provider && install -m 0755 /tmp/bench-domain-provider /usr/local/bin/bench-domain-provider); "
		f"bench-domain-provider 2>&1 | head -1"
	)
	stdout, stderr, code = guest(cmd)
	assert code == 0 or "usage:" in stdout, f"installing bench-domain-provider failed: {stderr[-300:]}"


def _assert_bench_self_routing(
	server_name: str, site_vm_name: str, proxy_vm, region: str, domain: str
) -> None:
	"""Host-fact proof of spec/18's ONE-WAY PUSH model on the live site VM (a bench VM),
	driven by the REAL in-guest `bench-domain-provider` binary over IPv6 — the only run
	that can prove the trust root (the controller resolves the VM from the request's v6
	source /128, no parameter):

	  1. `register <fqdn>` from inside the guest reserves the name BEFORE `bench new-site`
	     (peeling the wildcard suffix to the bare label; the row appears on register, not
	     after create) and the PROXY serves it.
	  2. A FORCED `bench new-site` failure → the binary's `deregister` rollback leaves
	     NO stale Subdomain.
	  3. `bench drop-site` then `deregister` → the route DROPS from the proxy live map.
	  4. The host-level queries answer: `wildcard-domains` = the region wildcard,
	     `proxy-servers` includes this proxy's public IP (the Phase-1 verbs).
	  5. A direct `VirtualMachine.terminate` leaves NO Subdomain (Component F, total).

	The site VM's own v6 is the routing target. There is NO pull, NO sweeper, and the
	controller never SSHes the guest to discover routes — every write is the guest's
	`bench-domain-provider` POST, arbitrated controller-side. The converge LOGIC is
	unit-covered (test_bench_routing); this run proves the IPv6 origin + the proxy serving
	it. The binary takes the FULL FQDN and peels the region wildcard suffix to the label;
	`register` is FAIL-CLOSED (a transport error aborts the create)."""
	from atlas.atlas.ssh import connection_for_guest, run_ssh, ssh_key_file

	label = _BENCH_SELF_ROUTE_SUBDOMAIN
	fqdn = f"{label}.{domain}"
	site_v6 = frappe.get_doc("Virtual Machine", site_vm_name).ipv6_address
	acme_map = {_TEST_SUBDOMAIN: site_v6}
	connection = connection_for_guest(frappe.get_doc("Virtual Machine", site_vm_name))

	def guest(command: str, timeout: int = 600) -> tuple[str, str, int]:
		with ssh_key_file(connection.ssh_private_key) as key_path:
			return run_ssh(connection, key_path, command, timeout_seconds=timeout)

	# The golden may carry an older guest binary (built from a previous bench/ tree), so
	# install the repo's CURRENT bench-domain-provider in place before driving it — the
	# run must exercise the Phase-1 contract, not whatever was baked.
	_install_repo_provider(guest)

	bench = "/home/frappe/pilot/benches/atlas"

	def new_site_cmd(site_fqdn: str) -> str:
		return (
			f'sudo -u frappe bash -lc "export PATH=/home/frappe/pilot:$PATH; cd {bench}; '
			f'bench -b atlas new-site {site_fqdn} --admin-password atlas-baked --apps erpnext"'
		)

	# 1. REGISTER reserves the name BEFORE the create (block-at-create by ordering). The
	#    POST traverses IPv6, so the controller resolves THIS VM from its v6 source /128
	#    — no VM-identifying argument. Assert the row exists IMMEDIATELY (the
	#    authoritative insert is register's, not a later pull), points at this VM, and is
	#    active; then create the local site and prove the PROXY serves it end to end.
	assert not frappe.db.exists("Subdomain", label), "a stale Subdomain exists before register"
	_stdout, stderr, code = guest(f"bench-domain-provider register {fqdn}")
	assert code == 0, f"guest bench-domain-provider register failed: {stderr[-500:]}"
	row = frappe.get_doc("Subdomain", label)
	assert row.virtual_machine == site_vm_name and row.active, (
		f"register did not reserve {label} for this VM (vm={row.virtual_machine}, active={row.active})"
	)
	# The reservation resolved this VM by its v6 source: the audit row carries this VM
	# and its /128 (the trust root, proven on the real origin).
	audit = frappe.get_all(
		"Bench Routing Audit",
		filters={"endpoint": "register", "label": label, "status": "ok"},
		fields=["vm", "source_ip"],
	)
	assert audit and audit[0]["vm"] == site_vm_name, f"register audit did not resolve this VM: {audit}"
	assert audit[0]["source_ip"] == site_v6, (
		f"register resolved a source /128 ({audit[0]['source_ip']}) that is not this VM's v6 ({site_v6})"
	)
	_stdout, stderr, code = guest(new_site_cmd(fqdn))
	assert code == 0, f"guest new-site failed: {stderr[-500:]}"
	proxy.reconcile_proxy(proxy_vm.name)
	_assert_live_map(proxy_vm.name, {**acme_map, label: site_v6})
	print(f"[e2e] bench self-routing: guest-reserved {fqdn} SERVED by the proxy OK")

	# 2. CREATE-FAILURE ROLLBACK: register a fresh label, force the create to fail
	#    (a deliberately invalid new-site), then deregister — the rollback — and assert
	#    NO stale Subdomain survives. This is the orphan-free path (register first,
	#    deregister on create-failure).
	rollback_label = f"{label}-fail"
	rollback_fqdn = f"{rollback_label}.{domain}"
	_stdout, stderr, code = guest(f"bench-domain-provider register {rollback_fqdn}")
	assert code == 0, f"register for the rollback case failed: {stderr[-500:]}"
	assert frappe.db.exists("Subdomain", rollback_label), "register did not reserve the rollback label"
	# A new-site with a bogus app name fails AFTER the reservation, the create-failure case.
	bogus = (
		f'sudo -u frappe bash -lc "export PATH=/home/frappe/pilot:$PATH; cd {bench}; '
		f'bench -b atlas new-site {rollback_label}.{domain} --admin-password atlas-baked --apps no_such_app_xyz"'
	)
	_stdout, _stderr, fail_code = guest(bogus)
	assert fail_code != 0, "the forced new-site failure unexpectedly succeeded"
	_stdout, stderr, code = guest(f"bench-domain-provider deregister {rollback_fqdn}")
	assert code == 0, f"rollback deregister failed: {stderr[-500:]}"
	assert not frappe.db.exists("Subdomain", rollback_label), (
		"the create-failure rollback left a stale Subdomain"
	)
	print("[e2e] bench self-routing: create-failure rollback left no stale route OK")

	# 3. DROP + DEREGISTER: drop the site in the guest, then deregister — the route DROPS
	#    from the proxy live map (deregister's on_trash deconverges). `drop-site` drops
	#    the site DB, so it needs the MariaDB ROOT password; bench-cli prompts for it over
	#    a non-interactive SSH session, so pipe in the baked, shared root password.
	drop = (
		f'sudo -u frappe bash -lc "export PATH=/home/frappe/pilot:$PATH; cd {bench}; '
		f'echo {_BAKED_MARIADB_ROOT_PASSWORD} | bench -b atlas drop-site {fqdn} --no-backup --force"'
	)
	_stdout, stderr, code = guest(drop)
	assert code == 0, f"guest drop-site failed: {stderr[-500:]}"
	_stdout, stderr, code = guest(f"bench-domain-provider deregister {fqdn}")
	assert code == 0, f"guest deregister failed: {stderr[-500:]}"
	assert not frappe.db.exists("Subdomain", label), "deregister did not delete the dropped site's route"
	proxy.reconcile_proxy(proxy_vm.name)
	_assert_live_map(proxy_vm.name, acme_map)
	print(f"[e2e] bench self-routing: dropped+deregistered {fqdn} STOPPED serving at the proxy OK")

	# 4. HOST-LEVEL QUERIES (the Phase-1 verbs that replaced `list`): `wildcard-domains`
	#    returns the region wildcard pilot constrains site names to, and `proxy-servers`
	#    returns this proxy's public IP — the edge pilot locks its nginx down to, closing
	#    the spec/18 trust-root gap. Both are host-level (no VM-identifying arg) and exit 0
	#    with a JSON list on stdout. (The old `list`/stray-clear check retired: the new
	#    contract has no `list` verb — pilot drives `deregister` itself on drop.)
	stdout, stderr, code = guest("bench-domain-provider wildcard-domains")
	assert code == 0, f"guest wildcard-domains failed: {stderr[-500:]}"
	wildcards = json.loads(stdout.strip())
	assert wildcards == [f"*.{domain}"], f"wildcard-domains returned {wildcards!r}, expected ['*.{domain}']"
	stdout, stderr, code = guest("bench-domain-provider proxy-servers")
	assert code == 0, f"guest proxy-servers failed: {stderr[-500:]}"
	ips = json.loads(stdout.strip())
	assert proxy_vm.ipv6_address in ips, (
		f"proxy-servers {ips!r} does not include this proxy's v6 {proxy_vm.ipv6_address!r}"
	)
	print(
		f"[e2e] bench self-routing: host queries wildcard-domains={wildcards} proxy-servers⊇{proxy_vm.ipv6_address} OK"
	)

	# 5. TERMINATE (Component F, the only controller-side teardown): re-register a route
	#    so the VM owns one, then a direct VM terminate must leave NO Subdomain for it.
	_stdout, _stderr, _code = guest(f"bench-domain-provider register {fqdn}")
	assert frappe.db.count("Subdomain", {"virtual_machine": site_vm_name}) > 0, (
		"expected the VM to own a Subdomain before terminate"
	)
	frappe.db.commit()
	frappe.get_doc("Virtual Machine", site_vm_name).terminate()
	assert frappe.db.count("Subdomain", {"virtual_machine": site_vm_name}) == 0, (
		"VirtualMachine.terminate left a stale Subdomain"
	)
	print("[e2e] bench self-routing: terminate cleaned up the VM's Subdomains OK")


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


# --- Central create_site on-ramp -----------------------------------------


def _create_site(team: str, subdomain: str) -> str:
	"""Drive the real Central-facing endpoint and return the created Site's FQDN.

	`create_site` get-or-creates the Tenant (keyed on `team`) and inserts the Site
	(Pending); the Site's after_insert enqueues auto_provision on the worker — the
	whole clone→deploy→route chain under test. Asserts the returned mirror is shaped
	the way Central reflects it."""
	from atlas.atlas.api.site import create_site

	result = create_site(team=team, subdomain=subdomain)
	frappe.db.commit()
	fqdn = result["name"]
	assert frappe.db.exists("Site", fqdn), f"create_site did not insert a Site: {result}"
	assert result["status"] == "Pending", f"new site should start Pending: {result}"
	assert result["team"] == team, f"mirror missing team: {result}"
	tenant = frappe.db.get_value("Site", fqdn, "tenant")
	assert tenant, f"Site {fqdn} not stamped with a Tenant"
	print(f"[e2e] create_site -> Site {fqdn} (Pending) for tenant {tenant}; auto_provision enqueued")
	return fqdn


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


def _assert_login_url_set(fqdn: str) -> None:
	"""The tenant handoff is a one-click login URL, NOT a password — `deploy-site.py`
	mints it via `bench browse --user Administrator --sid` (a real 24h session) and
	the Site controller stores it before the readiness wait, surfaced to Central
	(the site.status_changed event + get_site poll). Assert it carries the FQDN and
	a `sid` — proof the guest's `bench browse` call actually ran and returned a
	real token, not just that SOME string got stored. We don't follow the URL here:
	LE staging is untrusted (curl -k) and a real Desk login adds nothing this proves
	over the 200 + a well-formed minted URL."""
	login_url = frappe.get_doc("Site", fqdn).get("login_url")
	assert login_url, f"Site {fqdn} has no login_url stored after Running"
	assert login_url.startswith(f"https://{fqdn}/app?sid="), (
		f"login_url has an unexpected shape: {login_url!r}"
	)
	assert login_url.rsplit("sid=", 1)[-1], f"login_url has an empty sid: {login_url!r}"
	print(f"[e2e] login URL stored on {fqdn} OK")


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
	team: str,
) -> None:
	"""Billable-aware teardown, every step guarded so one failure doesn't strand the
	rest (terminate the site VM, delete the Subdomain, release the reserved IP,
	delete the Site row, and the created Tenant — which persists past the
	transaction). Site.terminate() already drops the Subdomain + the backing VM, so
	we drive it first, then mop up the rows it doesn't own."""
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

	# 2. The Tenant create_site get-or-created (a non-transactional row that
	#    outlives the run). The Site that linked it is gone from step 1.
	# The Tenant `name` *is* the Central `Team.name`.
	tenant = team if frappe.db.exists("Tenant", team) else None
	if tenant:
		try:
			frappe.delete_doc("Tenant", tenant, force=1, ignore_permissions=True)
		except Exception:
			_warn(f"Tenant {tenant} delete failed")
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
